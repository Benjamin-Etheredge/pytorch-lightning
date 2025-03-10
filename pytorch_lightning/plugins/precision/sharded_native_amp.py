# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Union

from pytorch_lightning.plugins.precision.native_amp import NativeMixedPrecisionPlugin
from pytorch_lightning.utilities import _FAIRSCALE_AVAILABLE, _NATIVE_AMP_AVAILABLE

if _NATIVE_AMP_AVAILABLE and _FAIRSCALE_AVAILABLE:
    from fairscale.optim import OSS
    from fairscale.optim.grad_scaler import ShardedGradScaler


class ShardedNativeMixedPrecisionPlugin(NativeMixedPrecisionPlugin):
    """Mixed Precision for Sharded Training."""

    def __init__(self, precision: Union[int, str] = 16, use_cpu: bool = False) -> None:
        super().__init__(precision, use_cpu=use_cpu)
        if not self.use_cpu:
            self.scaler = ShardedGradScaler()

    def clip_grad_by_norm(
        self, optimizer: "OSS", clip_val: Union[int, float], norm_type: float = 2.0, eps: float = 1e-6
    ) -> None:
        optimizer.clip_grad_norm(clip_val, norm_type=norm_type)
