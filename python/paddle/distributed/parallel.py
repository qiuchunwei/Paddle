# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except jin compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import six
import warnings
from multiprocessing import Process, Manager
import time
import sys

from paddle import compat as cpt

# deprecated module import
from paddle.fluid import core
from paddle.fluid.framework import _set_expected_place
from paddle.fluid.dygraph import parallel_helper
from paddle.fluid.dygraph.parallel import ParallelEnv
from paddle.distributed.fleet.base.private_helper_function import wait_server_ready

__all__ = ["init_parallel_env"]

ParallelStrategy = core.ParallelStrategy

# NOTE(chenweihang): Maintain a global parallel env to avoid 
# initializing ParallelEnv every time and improve performance
_global_parallel_env = None


def _get_global_parallel_env():
    global _global_parallel_env
    if _global_parallel_env is None:
        _global_parallel_env = ParallelEnv()
    return _global_parallel_env


def _start_kv_server(port, http_server_d, size):
    from paddle.distributed.fleet.utils.http_server import KVServer
    http_server = KVServer(int(port), size=size)
    http_server.start()
    wait_seconds = 3
    while http_server_d.get("running", False) or not http_server.should_stop():
        time.sleep(wait_seconds)
    http_server.stop()


def init_parallel_env():
    """
    Initialize parallel training environment in dynamic graph mode.

    .. note::
        Now initialize both `NCCL` and `GLOO` contexts for communication.

    Returns:
        None
        
    Examples:
        .. code-block:: python

            import paddle
            import paddle.nn as nn
            import paddle.optimizer as opt
            import paddle.distributed as dist

            class LinearNet(nn.Layer):
                def __init__(self):
                    super(LinearNet, self).__init__()
                    self._linear1 = nn.Linear(10, 10)
                    self._linear2 = nn.Linear(10, 1)
                    
                def forward(self, x):
                    return self._linear2(self._linear1(x))

            def train():
                # 1. initialize parallel environment
                dist.init_parallel_env()

                # 2. create data parallel layer & optimizer
                layer = LinearNet()
                dp_layer = paddle.DataParallel(layer)

                loss_fn = nn.MSELoss()
                adam = opt.Adam(
                    learning_rate=0.001, parameters=dp_layer.parameters())

                # 3. run layer
                inputs = paddle.randn([10, 10], 'float32')
                outputs = dp_layer(inputs)
                labels = paddle.randn([10, 1], 'float32')
                loss = loss_fn(outputs, labels)
                
                loss.backward()

                adam.step()
                adam.clear_grad()

            if __name__ == '__main__':
                dist.spawn(train)
    """

    # 0. get env & check world size
    global _global_parallel_env
    # when call init_parallel_env, need update `_global_parallel_env`
    _global_parallel_env = ParallelEnv()
    parallel_env = _global_parallel_env
    # if not parallel, `init_parallel_env` do nothing
    if parallel_env.world_size < 2:
        warnings.warn(
            "Currently not a parallel execution environment, `paddle.distributed.init_parallel_env` will not do anything."
        )
        return

    # 1. gpu check
    if not core.is_compiled_with_cuda():
        raise NotImplementedError(
            "Cannot initialize parallel environment in CPU-only version, now only "
            "supports initializing the GPU parallel environment. Please recompile "
            "or reinstall paddle with GPU support.")

    # 2. check env
    def _check_var_exists(var_name):
        var = os.environ.get(var_name, None)
        if var is None:
            raise ValueError("paddle.distributed initialize error, "
                             "environment variable %s is needed, but not set." %
                             var_name)

    _check_var_exists("FLAGS_selected_gpus")
    _check_var_exists("PADDLE_TRAINER_ID")
    _check_var_exists("PADDLE_CURRENT_ENDPOINT")
    _check_var_exists("PADDLE_TRAINERS_NUM")
    _check_var_exists("PADDLE_TRAINER_ENDPOINTS")

    # 3: init gloo context (step 1: httpsever start)
    ep_rank_0 = parallel_env.trainer_endpoints[0].split(":")
    ep_rank = parallel_env.trainer_endpoints[parallel_env.rank].split(":")
    manager = Manager()
    # glboal dict to store status
    http_server_d = manager.dict()
    http_server_d["running"] = False
    if parallel_env.rank == 0:
        # The scope for worker used by http server is '_worker'
        size = {'_worker': parallel_env.world_size}
        http_server = Process(
            target=_start_kv_server,
            args=(int(ep_rank_0[1]), http_server_d, size))
        http_server.daemon = True
        http_server_d["running"] = True
        http_server.start()

    # 4. init NCCL ParallelStrategy
    strategy = ParallelStrategy()
    if parallel_helper._is_parallel_ctx_initialized():
        warnings.warn("The parallel environment has been initialized.")
    strategy.nranks = parallel_env.world_size
    strategy.local_rank = parallel_env.rank
    strategy.trainer_endpoints = parallel_env.trainer_endpoints
    strategy.current_endpoint = parallel_env.current_endpoint

    # NOTE(chenweihang): [ why config global place here? ]
    # the dygraph mode will be set to default mode,
    # users will not call `dygraph.guard` or `enable_dygraph`
    # directly, if they want to switch default place,
    # they need to call a function to change default place,
    # here just set correctly place to users
    place = core.CUDAPlace(parallel_env.device_id)
    _set_expected_place(place)

    # init nccl context
    parallel_helper._set_parallel_ctx(core.NCCLParallelContext(strategy, place))
    parallel_helper._init_parallel_ctx()

    # 5: init gloo context (step 2: gloo init)
    # dividing init_gloo into two part beacause nccl and gloo
    # are separately looking for free ports which sometimes
    # leads to port-conflict.
    wait_server_ready([parallel_env.trainer_endpoints[0]])

    gloo_strategy = core.GlooParallelStrategy()
    gloo_strategy.rank = parallel_env.rank
    gloo_strategy.rank_num = parallel_env.world_size
    gloo_strategy.ip_address = ep_rank_0[0]
    gloo_strategy.ip_port = int(ep_rank_0[1])
    default_init_timeout_seconds = 3600
    default_run_timeout_seconds = 9999999
    gloo_strategy.init_seconds = default_init_timeout_seconds
    gloo_strategy.run_seconds = default_run_timeout_seconds
    gloo = core.GlooParallelContext(gloo_strategy)
    gloo.init()
    if parallel_env.rank == 0:
        http_server_d["running"] = False
        http_server.join()


def get_rank():
    """
    Returns the rank of current trainer.

    Its value is equal to the value of the environment variable ``PADDLE_TRAINER_ID`` . 
    The default value is 0.

    Returns:
        (int) The rank of current trainer.

    Examples:
        .. code-block:: python

            import paddle
            import paddle.distributed as dist

            # execute this command in terminal: export PADDLE_TRAINER_ID=0
            print("The rank is %d" % dist.get_rank())
            # The rank is 0
    """
    return _get_global_parallel_env().rank


def get_world_size():
    """
    Returns the number of trainers (number of processes participating in current job).

    Its value is equal to the value of the environment variable ``PADDLE_TRAINERS_NUM`` . 
    The default value is 1.

    Returns:
        (int) The number of trainers.

    Examples:
        .. code-block:: python

            import paddle
            import paddle.distributed as dist

            # execute this command in terminal: export PADDLE_TRAINERS_NUM=4
            print("The world_size is %d" % dist.get_world_size())
            # The world_size is 4
    """
    return _get_global_parallel_env().world_size
