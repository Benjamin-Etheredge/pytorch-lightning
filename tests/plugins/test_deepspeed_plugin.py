import contextlib
import json
import os
from typing import Any, Dict, Optional
from unittest import mock

import pytest
import torch
import torch.nn.functional as F
from torch import nn, Tensor
from torch.optim import Optimizer
from torch.utils.data import DataLoader
from torchmetrics import Accuracy

from pytorch_lightning import LightningDataModule, LightningModule, seed_everything, Trainer
from pytorch_lightning.callbacks import Callback, LearningRateMonitor, ModelCheckpoint
from pytorch_lightning.plugins import DeepSpeedPlugin, DeepSpeedPrecisionPlugin
from pytorch_lightning.plugins.training_type.deepspeed import LightningDeepSpeedModule
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.imports import _DEEPSPEED_AVAILABLE
from tests.helpers.boring_model import BoringModel, RandomDataset, RandomIterableDataset
from tests.helpers.datamodules import ClassifDataModule
from tests.helpers.runif import RunIf

if _DEEPSPEED_AVAILABLE:
    import deepspeed
    from deepspeed.utils.zero_to_fp32 import convert_zero_checkpoint_to_fp32_state_dict


class ModelParallelBoringModel(BoringModel):
    def __init__(self):
        super().__init__()
        self.layer = None

    def configure_sharded_model(self) -> None:
        self.layer = torch.nn.Linear(32, 2)

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        self.configure_sharded_model()


class ModelParallelBoringModelNoSchedulers(ModelParallelBoringModel):
    def configure_optimizers(self):
        return torch.optim.SGD(self.layer.parameters(), lr=0.1)


class ModelParallelBoringModelManualOptim(BoringModel):
    def __init__(self):
        super().__init__()
        self.layer = None

    def training_step(self, batch, batch_idx):
        opt = self.optimizers()
        output = self(batch)
        loss = self.loss(batch, output)
        opt.zero_grad()
        self.manual_backward(loss)
        opt.step()

    def configure_sharded_model(self) -> None:
        self.layer = torch.nn.Linear(32, 2)

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        self.configure_sharded_model()

    @property
    def automatic_optimization(self) -> bool:
        return False


def test_deepspeed_lightning_module(tmpdir):
    """Test to ensure that a model wrapped in `LightningDeepSpeedModule` moves types and device correctly."""

    model = BoringModel()
    module = LightningDeepSpeedModule(model, precision=16)

    module.half()
    assert module.dtype == torch.half
    assert model.dtype == torch.half

    module.to(torch.double)
    assert module.dtype == torch.double
    assert model.dtype == torch.double


@RunIf(min_gpus=1)
def test_deepspeed_lightning_module_precision(tmpdir):
    """Test to ensure that a model wrapped in `LightningDeepSpeedModule` moves tensors to half when precision
    16."""

    model = BoringModel()
    module = LightningDeepSpeedModule(model, precision=16)

    module.cuda().half()
    assert module.dtype == torch.half
    assert model.dtype == torch.half

    x = torch.randn((1, 32), dtype=torch.float).cuda()
    out = module(x)

    assert out.dtype == torch.half

    module.to(torch.double)
    assert module.dtype == torch.double
    assert model.dtype == torch.double


@pytest.fixture
def deepspeed_config():
    return {
        "optimizer": {"type": "SGD", "params": {"lr": 3e-5}},
        "scheduler": {
            "type": "WarmupLR",
            "params": {"last_batch_iteration": -1, "warmup_min_lr": 0, "warmup_max_lr": 3e-5, "warmup_num_steps": 100},
        },
    }


@pytest.fixture
def deepspeed_zero_config(deepspeed_config):
    return {**deepspeed_config, "zero_allow_untested_optimizer": True, "zero_optimization": {"stage": 2}}


@RunIf(deepspeed=True)
@pytest.mark.parametrize("input", ("deepspeed", DeepSpeedPlugin))
def test_deepspeed_plugin_string(tmpdir, input):
    """Test to ensure that the plugin can be passed via string or instance, and parallel devices is correctly
    set."""

    trainer = Trainer(fast_dev_run=True, default_root_dir=tmpdir, plugins=input if isinstance(input, str) else input())

    assert isinstance(trainer.accelerator.training_type_plugin, DeepSpeedPlugin)
    assert trainer.accelerator.training_type_plugin.parallel_devices == [torch.device("cpu")]


@RunIf(deepspeed=True)
def test_deepspeed_plugin_env(tmpdir, monkeypatch, deepspeed_config):
    """Test to ensure that the plugin can be passed via a string with an environment variable."""
    config_path = os.path.join(tmpdir, "temp.json")
    with open(config_path, "w") as f:
        f.write(json.dumps(deepspeed_config))
    monkeypatch.setenv("PL_DEEPSPEED_CONFIG_PATH", config_path)

    trainer = Trainer(fast_dev_run=True, default_root_dir=tmpdir, plugins="deepspeed")

    plugin = trainer.accelerator.training_type_plugin
    assert isinstance(plugin, DeepSpeedPlugin)
    assert plugin.parallel_devices == [torch.device("cpu")]
    assert plugin.config == deepspeed_config


@RunIf(amp_native=True, deepspeed=True)
@pytest.mark.parametrize("precision", [16, "mixed"])
@pytest.mark.parametrize(
    "amp_backend",
    [pytest.param("native", marks=RunIf(amp_native=True)), pytest.param("apex", marks=RunIf(amp_apex=True))],
)
def test_deepspeed_precision_choice(amp_backend, precision, tmpdir):
    """Test to ensure precision plugin is also correctly chosen.

    DeepSpeed handles precision via Custom DeepSpeedPrecisionPlugin
    """

    trainer = Trainer(
        fast_dev_run=True, default_root_dir=tmpdir, plugins="deepspeed", amp_backend=amp_backend, precision=precision
    )

    assert isinstance(trainer.accelerator.training_type_plugin, DeepSpeedPlugin)
    assert isinstance(trainer.accelerator.precision_plugin, DeepSpeedPrecisionPlugin)
    assert trainer.accelerator.precision_plugin.precision == precision


@RunIf(deepspeed=True)
def test_deepspeed_with_invalid_config_path(tmpdir):
    """Test to ensure if we pass an invalid config path we throw an exception."""

    with pytest.raises(
        MisconfigurationException, match="You passed in a path to a DeepSpeed config but the path does not exist"
    ):
        DeepSpeedPlugin(config="invalid_path.json")


@RunIf(deepspeed=True)
def test_deepspeed_with_env_path(tmpdir, monkeypatch, deepspeed_config):
    """Test to ensure if we pass an env variable, we load the config from the path."""
    config_path = os.path.join(tmpdir, "temp.json")
    with open(config_path, "w") as f:
        f.write(json.dumps(deepspeed_config))
    monkeypatch.setenv("PL_DEEPSPEED_CONFIG_PATH", config_path)
    plugin = DeepSpeedPlugin()
    assert plugin.config == deepspeed_config


@RunIf(deepspeed=True)
def test_deepspeed_defaults(tmpdir):
    """Ensure that defaults are correctly set as a config for DeepSpeed if no arguments are passed."""
    plugin = DeepSpeedPlugin()
    assert plugin.config is not None
    assert isinstance(plugin.config["zero_optimization"], dict)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_warn_deepspeed_override_backward(tmpdir):
    """Test to ensure that if the backward hook in the LightningModule is overridden, we throw a warning."""

    class TestModel(BoringModel):
        def backward(self, loss: Tensor, optimizer: Optimizer, optimizer_idx: int, *args, **kwargs) -> None:
            return loss.backward()

    model = TestModel()
    trainer = Trainer(fast_dev_run=True, default_root_dir=tmpdir, plugins=DeepSpeedPlugin(), gpus=1, precision=16)
    with pytest.warns(UserWarning, match="will be ignored since DeepSpeed handles the backward"):
        trainer.fit(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
@pytest.mark.parametrize(
    ["dataset_cls", "value"],
    [(RandomDataset, "auto"), (RandomDataset, 10), (RandomIterableDataset, "auto"), (RandomIterableDataset, 10)],
)
def test_deepspeed_auto_batch_size_config_select(tmpdir, dataset_cls, value):
    """Test to ensure that the batch size is correctly set as expected for deepspeed logging purposes."""

    class TestModel(BoringModel):
        def train_dataloader(self):
            return DataLoader(dataset_cls(32, 64))

    class AssertCallback(Callback):
        def on_train_start(self, trainer, pl_module) -> None:
            assert isinstance(trainer.accelerator.training_type_plugin, DeepSpeedPlugin)
            config = trainer.accelerator.training_type_plugin.config

            # int value overrides auto mode
            expected_value = value if isinstance(value, int) else 1
            if dataset_cls == RandomDataset:
                expected_value = pl_module.train_dataloader().batch_size if value == "auto" else value

            assert config["train_micro_batch_size_per_gpu"] == expected_value
            raise SystemExit

    ck = AssertCallback()
    model = TestModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        fast_dev_run=True,
        callbacks=ck,
        gpus=1,
        plugins=DeepSpeedPlugin(logging_batch_size_per_gpu=value, zero_optimization=False),
    )
    with pytest.raises(SystemExit):
        trainer.fit(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_run_configure_optimizers(tmpdir):
    """Test end to end that deepspeed works with defaults (without ZeRO as that requires compilation), whilst using
    configure_optimizers for optimizers and schedulers."""

    class TestCB(Callback):
        def on_train_start(self, trainer, pl_module) -> None:
            from deepspeed.runtime.zero.stage2 import FP16_DeepSpeedZeroOptimizer

            assert isinstance(trainer.optimizers[0], FP16_DeepSpeedZeroOptimizer)
            assert isinstance(trainer.optimizers[0].optimizer, torch.optim.SGD)
            assert isinstance(trainer.lr_schedulers[0]["scheduler"], torch.optim.lr_scheduler.StepLR)
            # check that the lr_scheduler config was preserved
            assert trainer.lr_schedulers[0]["name"] == "Sean"
            # Ensure DeepSpeed engine has initialized with our lr_scheduler
            assert isinstance(trainer.model.lr_scheduler, torch.optim.lr_scheduler.StepLR)

    class TestModel(BoringModel):
        def configure_optimizers(self):
            [optimizer], [scheduler] = super().configure_optimizers()
            return {"optimizer": optimizer, "lr_scheduler": {"scheduler": scheduler, "name": "Sean"}}

    model = TestModel()
    lr_monitor = LearningRateMonitor()
    trainer = Trainer(
        plugins=DeepSpeedPlugin(),  # disable ZeRO so our optimizers are not wrapped
        default_root_dir=tmpdir,
        gpus=1,
        fast_dev_run=True,
        precision=16,
        callbacks=[TestCB(), lr_monitor],
    )
    trainer.fit(model)

    assert lr_monitor.lrs == {"Sean": [0.1]}

    _assert_save_model_is_equal(model, tmpdir, trainer)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_config(tmpdir, deepspeed_zero_config):
    """Test to ensure deepspeed works correctly when passed a DeepSpeed config object including
    optimizers/schedulers and saves the model weights to load correctly."""

    class TestCB(Callback):
        def on_train_start(self, trainer, pl_module) -> None:
            from deepspeed.runtime.lr_schedules import WarmupLR
            from deepspeed.runtime.zero.stage2 import FP16_DeepSpeedZeroOptimizer

            assert isinstance(trainer.optimizers[0], FP16_DeepSpeedZeroOptimizer)
            assert isinstance(trainer.optimizers[0].optimizer, torch.optim.SGD)
            assert isinstance(trainer.lr_schedulers[0]["scheduler"], WarmupLR)
            # Ensure DeepSpeed engine has initialized with our lr_scheduler
            assert isinstance(trainer.model.lr_scheduler, WarmupLR)

    model = BoringModel()
    trainer = Trainer(
        plugins=[DeepSpeedPlugin(config=deepspeed_zero_config)],
        default_root_dir=tmpdir,
        gpus=1,
        fast_dev_run=True,
        precision=16,
        callbacks=[TestCB()],
    )

    trainer.fit(model)
    trainer.test(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_custom_precision_params(tmpdir):
    """Ensure if we modify the FP16 parameters via the DeepSpeedPlugin, the deepspeed config contains these
    changes."""

    class TestCB(Callback):
        def on_train_start(self, trainer, pl_module) -> None:
            assert trainer.training_type_plugin.config["fp16"]["loss_scale"] == 10
            assert trainer.training_type_plugin.config["fp16"]["initial_scale_power"] == 10
            assert trainer.training_type_plugin.config["fp16"]["loss_scale_window"] == 10
            assert trainer.training_type_plugin.config["fp16"]["hysteresis"] == 10
            assert trainer.training_type_plugin.config["fp16"]["min_loss_scale"] == 10
            raise SystemExit()

    model = BoringModel()
    ds = DeepSpeedPlugin(loss_scale=10, initial_scale_power=10, loss_scale_window=10, hysteresis=10, min_loss_scale=10)
    trainer = Trainer(default_root_dir=tmpdir, plugins=[ds], precision=16, gpus=1, callbacks=[TestCB()])
    with pytest.raises(SystemExit):
        trainer.fit(model)


@RunIf(deepspeed=True)
def test_deepspeed_custom_activation_checkpointing_params(tmpdir):
    """Ensure if we modify the activation checkpointing parameters, the deepspeed config contains these changes."""
    ds = DeepSpeedPlugin(
        partition_activations=True,
        cpu_checkpointing=True,
        contiguous_memory_optimization=True,
        synchronize_checkpoint_boundary=True,
    )
    checkpoint_config = ds.config["activation_checkpointing"]
    assert checkpoint_config["partition_activations"]
    assert checkpoint_config["cpu_checkpointing"]
    assert checkpoint_config["contiguous_memory_optimization"]
    assert checkpoint_config["synchronize_checkpoint_boundary"]


@RunIf(min_gpus=1, deepspeed=True)
def test_deepspeed_assert_config_zero_offload_disabled(tmpdir, deepspeed_zero_config):
    """Ensure if we use a config and turn off offload_optimizer, that this is set to False within the config."""

    deepspeed_zero_config["zero_optimization"]["offload_optimizer"] = False

    class TestCallback(Callback):
        def on_before_accelerator_backend_setup(self, trainer, pl_module) -> None:
            assert trainer.training_type_plugin.config["zero_optimization"]["offload_optimizer"] is False
            raise SystemExit()

    model = BoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        progress_bar_refresh_rate=0,
        max_epochs=1,
        plugins=[DeepSpeedPlugin(config=deepspeed_zero_config)],
        precision=16,
        gpus=1,
        callbacks=[TestCallback()],
    )
    with pytest.raises(SystemExit):
        trainer.fit(model)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu(tmpdir):
    """Test to ensure that DeepSpeed with multiple GPUs works and deepspeed distributed is initialized
    correctly."""
    model = BoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=2, fast_dev_run=True, precision=16
    )
    with mock.patch("deepspeed.init_distributed", wraps=deepspeed.init_distributed) as mock_deepspeed_distributed:
        trainer.fit(model)
    mock_deepspeed_distributed.assert_called_once()
    trainer.test(model)

    _assert_save_model_is_equal(model, tmpdir, trainer)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_fp32_works(tmpdir):
    model = BoringModel()
    trainer = Trainer(default_root_dir=tmpdir, gpus=1, plugins="deepspeed_stage_3", fast_dev_run=True)
    trainer.fit(model)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_stage_3_save_warning(tmpdir):
    """Test to ensure that DeepSpeed Stage 3 gives a warning when saving on rank zero."""
    model = BoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=2, fast_dev_run=True, precision=16
    )
    trainer.fit(model)
    checkpoint_path = os.path.join(tmpdir, "model.pt")

    # both ranks need to call save checkpoint, however only rank 0 needs to check the warning
    context_manager = (
        pytest.warns(UserWarning, match="each worker will save a shard of the checkpoint within a directory.")
        if trainer.is_global_zero
        else contextlib.suppress()
    )
    with context_manager:
        trainer.save_checkpoint(checkpoint_path)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_multigpu_single_file(tmpdir):
    """Test to ensure that DeepSpeed loads from a single file checkpoint."""
    model = BoringModel()
    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True)
    trainer.fit(model)
    trainer.save_checkpoint(checkpoint_path)

    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=1, fast_dev_run=True, precision=16
    )
    plugin = trainer.training_type_plugin
    assert isinstance(plugin, DeepSpeedPlugin)
    assert not plugin.load_full_weights
    with pytest.raises(MisconfigurationException, match="DeepSpeed was unable to load the checkpoint."):
        trainer.test(model, ckpt_path=checkpoint_path)

    trainer = Trainer(
        default_root_dir=tmpdir,
        plugins=[DeepSpeedPlugin(stage=3, load_full_weights=True)],
        gpus=1,
        fast_dev_run=True,
        precision=16,
    )
    plugin = trainer.training_type_plugin
    assert isinstance(plugin, DeepSpeedPlugin)
    assert plugin.load_full_weights
    trainer.test(model, ckpt_path=checkpoint_path)


class ModelParallelClassificationModel(LightningModule):
    def __init__(self, lr: float = 0.01, num_blocks: int = 5):
        super().__init__()
        self.lr = lr
        self.num_blocks = num_blocks
        self.prepare_data_per_node = True

        self.train_acc = Accuracy()
        self.valid_acc = Accuracy()
        self.test_acc = Accuracy()

    def make_block(self):
        return nn.Sequential(nn.Linear(32, 32, bias=False), nn.ReLU())

    def configure_sharded_model(self) -> None:
        self.model = nn.Sequential(*(self.make_block() for x in range(self.num_blocks)), nn.Linear(32, 3))

    def forward(self, x):
        x = self.model(x)
        # Ensure output is in float32 for softmax operation
        x = x.float()
        logits = F.softmax(x, dim=1)
        return logits

    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = F.cross_entropy(logits, y)
        self.log("train_loss", loss, prog_bar=True)
        self.log("train_acc", self.train_acc(logits, y), prog_bar=True, sync_dist=True)
        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        self.log("val_loss", F.cross_entropy(logits, y), prog_bar=False, sync_dist=True)
        self.log("val_acc", self.valid_acc(logits, y), prog_bar=True, sync_dist=True)

    def test_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        self.log("test_loss", F.cross_entropy(logits, y), prog_bar=False, sync_dist=True)
        self.log("test_acc", self.test_acc(logits, y), prog_bar=True, sync_dist=True)

    def predict_step(self, batch, batch_idx, dataloader_idx=None):
        x, y = batch
        logits = self.forward(x)
        self.test_acc(logits, y)
        return self.test_acc.compute()

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)

        lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.99)
        return [optimizer], [{"scheduler": lr_scheduler, "interval": "step"}]

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        if not hasattr(self, "model"):
            self.configure_sharded_model()


class ManualModelParallelClassificationModel(ModelParallelClassificationModel):
    @property
    def automatic_optimization(self) -> bool:
        return False

    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = F.cross_entropy(logits, y)
        opt = self.optimizers()
        self.log("train_loss", loss, prog_bar=True)
        self.log("train_acc", self.train_acc(logits, y), prog_bar=True, sync_dist=True)
        opt.zero_grad()
        self.manual_backward(loss)
        opt.step()


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_3(tmpdir, deepspeed_config):
    """Test to ensure ZeRO Stage 3 works with a parallel model."""
    model = ModelParallelBoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=2, fast_dev_run=True, precision=16
    )
    trainer.fit(model)
    trainer.test(model)

    _assert_save_model_is_equal(model, tmpdir, trainer)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_3_manual_optimization(tmpdir, deepspeed_config):
    """Test to ensure ZeRO Stage 3 works with a parallel model."""
    model = ModelParallelBoringModelManualOptim()
    model.training_epoch_end = None
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=2, fast_dev_run=True, precision=16
    )
    trainer.fit(model)
    trainer.test(model)

    _assert_save_model_is_equal(model, tmpdir, trainer)


def run_checkpoint_test(tmpdir: str, automatic_optimization: bool = True, accumulate_grad_batches: int = 2):
    seed_everything(1)
    if automatic_optimization:
        model = ModelParallelClassificationModel()
    else:
        model = ManualModelParallelClassificationModel()
    dm = ClassifDataModule()
    ck = ModelCheckpoint(monitor="val_acc", mode="max", save_last=True, save_top_k=-1)
    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=10,
        plugins=[DeepSpeedPlugin(stage=3)],
        gpus=2,
        precision=16,
        accumulate_grad_batches=accumulate_grad_batches,
        callbacks=[ck],
    )
    trainer.fit(model, datamodule=dm)

    results = trainer.test(datamodule=dm)
    assert results[0]["test_acc"] > 0.7
    saved_results = trainer.test(ckpt_path=ck.best_model_path, datamodule=dm)
    assert saved_results[0]["test_acc"] > 0.7
    assert saved_results == results

    if automatic_optimization:
        model = ModelParallelClassificationModel()
    else:
        model = ManualModelParallelClassificationModel()
    trainer = Trainer(default_root_dir=tmpdir, gpus=2, plugins=[DeepSpeedPlugin(stage=3)], precision=16)

    results = trainer.test(model, datamodule=dm, ckpt_path=ck.best_model_path)
    assert results[0]["test_acc"] > 0.7


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_3_checkpointing(tmpdir):
    """Test to ensure with Stage 3 and multiple GPUs that we can save/load a model resuming from a checkpoint, and
    see convergence."""
    run_checkpoint_test(tmpdir)


@RunIf(min_gpus=1, deepspeed=True, special=False)
def test_deepspeed_multigpu_stage_3_warns_resume_training(tmpdir):
    """Test to ensure with Stage 3 and multiple GPUs that we can resume from training, throwing a warning that the
    optimizer state and scheduler states cannot be restored."""
    dm = ClassifDataModule()
    model = BoringModel()
    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True)
    trainer.fit(model)
    trainer.save_checkpoint(checkpoint_path)

    trainer = Trainer(
        default_root_dir=tmpdir,
        fast_dev_run=True,
        plugins=DeepSpeedPlugin(stage=3, load_full_weights=True),
        gpus=1,
        precision=16,
        resume_from_checkpoint=checkpoint_path,
    )
    with pytest.warns(
        UserWarning,
        match="A single checkpoint file has been given. This means optimizer states and "
        "scheduler states can not be restored. If you'd like to restore these states, you must "
        "provide a path to the originally saved DeepSpeed checkpoint.",
    ):
        trainer.fit(model, datamodule=dm)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_3_resume_training(tmpdir):
    """Test to ensure with Stage 3 and multiple GPUs that we can resume training."""
    initial_model = ModelParallelClassificationModel()
    dm = ClassifDataModule()

    ck = ModelCheckpoint(monitor="val_acc", mode="max", save_last=True, save_top_k=-1)
    initial_trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=1,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        plugins=DeepSpeedPlugin(stage=3),
        gpus=1,
        precision=16,
        callbacks=[ck],
    )
    initial_trainer.fit(initial_model, datamodule=dm)

    class TestCallback(Callback):
        def on_train_batch_start(
            self, trainer: Trainer, pl_module: LightningModule, batch: Any, batch_idx: int, dataloader_idx: int
        ) -> None:
            original_deepspeed_plugin = initial_trainer.accelerator.training_type_plugin
            current_deepspeed_plugin = trainer.accelerator.training_type_plugin

            assert isinstance(original_deepspeed_plugin, DeepSpeedPlugin)
            assert isinstance(current_deepspeed_plugin, DeepSpeedPlugin)
            # assert optimizer states are the correctly loaded
            original_optimizer_dict = original_deepspeed_plugin.deepspeed_engine.optimizer.state_dict()
            current_optimizer_dict = current_deepspeed_plugin.deepspeed_engine.optimizer.state_dict()
            for orig_tensor, current_tensor in zip(
                original_optimizer_dict["fp32_flat_groups"], current_optimizer_dict["fp32_flat_groups"]
            ):
                assert torch.all(orig_tensor.eq(current_tensor))
            # assert model state is loaded correctly
            for current_param, initial_param in zip(pl_module.parameters(), initial_model.parameters()):
                assert torch.equal(current_param.cpu(), initial_param.cpu())
            # assert epoch has correctly been restored
            assert trainer.current_epoch == 1

    model = ModelParallelClassificationModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        fast_dev_run=True,
        plugins=DeepSpeedPlugin(stage=3),
        gpus=1,
        precision=16,
        resume_from_checkpoint=ck.best_model_path,
        callbacks=TestCallback(),
    )
    trainer.fit(model, datamodule=dm)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_3_checkpointing_full_weights_manual(tmpdir):
    """Test to ensure with Stage 3 and multiple GPUs that we can save/load a model resuming from a checkpoint,
    where we save the full weights to one file."""
    run_checkpoint_test(tmpdir, automatic_optimization=False, accumulate_grad_batches=1)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_2_accumulated_grad_batches(tmpdir):
    _deepspeed_multigpu_stage_2_accumulated_grad_batches(tmpdir, offload_optimizer=False)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_stage_2_accumulated_grad_batches_offload_optimizer(tmpdir):
    _deepspeed_multigpu_stage_2_accumulated_grad_batches(tmpdir, offload_optimizer=True)


def _deepspeed_multigpu_stage_2_accumulated_grad_batches(tmpdir, offload_optimizer):
    """Test to ensure with Stage 2 and multiple GPUs, accumulated grad batches works."""
    seed_everything(42)

    class VerificationCallback(Callback):
        def __init__(self):
            self.on_train_batch_start_called = False

        def on_train_batch_start(
            self, trainer, pl_module: LightningModule, batch: Any, batch_idx: int, dataloader_idx: int
        ) -> None:
            deepspeed_engine = trainer.training_type_plugin.model
            assert trainer.global_step == deepspeed_engine.global_steps
            self.on_train_batch_start_called = True

    model = ModelParallelClassificationModel()
    dm = ClassifDataModule()
    verification_callback = VerificationCallback()
    trainer = Trainer(
        default_root_dir=tmpdir,
        progress_bar_refresh_rate=0,
        # TODO: this test fails with max_epochs >1 as there are leftover batches per epoch.
        # there's divergence in how Lightning handles the last batch of the epoch with how DeepSpeed does it.
        # we step the optimizers on the last batch but DeepSpeed keeps the accumulation for the next epoch
        max_epochs=1,
        plugins=[DeepSpeedPlugin(stage=2, offload_optimizer=offload_optimizer)],
        gpus=2,
        limit_train_batches=5,
        limit_val_batches=2,
        precision=16,
        accumulate_grad_batches=2,
        callbacks=[verification_callback],
    )
    assert trainer.limit_train_batches % trainer.accumulate_grad_batches != 0, "leftover batches should be tested"
    trainer.fit(model, datamodule=dm)
    assert verification_callback.on_train_batch_start_called


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_test(tmpdir):
    """Test to ensure we can use DeepSpeed with just test using ZeRO Stage 3."""
    model = ModelParallelBoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=2, fast_dev_run=True, precision=16
    )
    trainer.test(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_multigpu_partial_partition_parameters(tmpdir):
    """Test to ensure that a module that defines a layer inside the ``__init__`` and ``configure_sharded_model``
    correctly converts all parameters to float16 when ``precision=16`` and runs successfully."""

    class TestModel(ModelParallelBoringModel):
        def __init__(self):
            super().__init__()
            self.layer_2 = torch.nn.Linear(32, 32)

        def configure_sharded_model(self) -> None:
            self.layer = torch.nn.Linear(32, 2)

        def forward(self, x):
            x = self.layer_2(x)
            return self.layer(x)

        def on_train_epoch_start(self) -> None:
            assert all([x.dtype == torch.float16 for x in self.parameters()])

    model = TestModel()
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=1, fast_dev_run=True, precision=16
    )
    trainer.fit(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_multigpu_test_rnn(tmpdir):
    """Test to ensure that turning off explicit partitioning of the entire module for ZeRO Stage 3 works when
    training with certain layers which will crash with explicit partitioning."""

    class TestModel(BoringModel):
        def __init__(self):
            super().__init__()
            self.rnn = torch.nn.GRU(32, 32)

        def on_train_epoch_start(self) -> None:
            assert all([x.dtype == torch.float16 for x in self.parameters()])

    model = TestModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        plugins=[DeepSpeedPlugin(stage=3, partition_module=False)],
        gpus=1,
        fast_dev_run=True,
        precision=16,
    )
    trainer.fit(model)


@RunIf(deepspeed=True)
@mock.patch("deepspeed.init_distributed", autospec=True)
@pytest.mark.parametrize("platform", ["Linux", "Windows"])
def test_deepspeed_plugin_env_variables(mock_deepspeed_distributed, tmpdir, platform):
    """Test to ensure that we setup distributed communication using correctly.

    When using windows, ranks environment variables should not be set, and deepspeed should handle this.
    """
    trainer = Trainer(default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)])
    plugin = trainer.training_type_plugin
    assert isinstance(plugin, DeepSpeedPlugin)
    with mock.patch("platform.system", return_value=platform) as mock_platform:
        plugin._init_deepspeed_distributed()
    mock_deepspeed_distributed.assert_called()
    mock_platform.assert_called()
    if platform == "Windows":
        # assert no env variables have been set within the DeepSpeedPlugin
        assert all(k not in os.environ for k in ("MASTER_PORT", "MASTER_ADDR", "RANK", "WORLD_SIZE", "LOCAL_RANK"))
    else:
        assert os.environ["MASTER_ADDR"] == str(trainer.training_type_plugin.cluster_environment.master_address())
        assert os.environ["MASTER_PORT"] == str(trainer.training_type_plugin.cluster_environment.master_port())
        assert os.environ["RANK"] == str(trainer.training_type_plugin.global_rank)
        assert os.environ["WORLD_SIZE"] == str(trainer.training_type_plugin.world_size)
        assert os.environ["LOCAL_RANK"] == str(trainer.training_type_plugin.local_rank)


def _assert_save_model_is_equal(model, tmpdir, trainer):
    checkpoint_path = os.path.join(tmpdir, "model.pt")
    checkpoint_path = trainer.accelerator.broadcast(checkpoint_path)
    trainer.save_checkpoint(checkpoint_path)
    trainer.accelerator.barrier()

    # carry out the check only on rank 0
    if trainer.is_global_zero:
        single_ckpt_path = os.path.join(tmpdir, "single_model.pt")
        convert_zero_checkpoint_to_fp32_state_dict(checkpoint_path, single_ckpt_path)
        state_dict = torch.load(single_ckpt_path)

        model = model.cpu()
        # Assert model parameters are identical after loading
        for orig_param, saved_model_param in zip(model.parameters(), state_dict.values()):
            if model.dtype == torch.half:
                # moved model to float32 for comparison with single fp32 saved weights
                saved_model_param = saved_model_param.half()
            assert torch.equal(orig_param, saved_model_param)


@RunIf(min_gpus=2, deepspeed=True, special=True)
def test_deepspeed_multigpu_no_schedulers(tmpdir):
    """Test to ensure ZeRO Stage 3 works with a parallel model and no schedulers."""
    model = ModelParallelBoringModelNoSchedulers()
    trainer = Trainer(
        default_root_dir=tmpdir, plugins=[DeepSpeedPlugin(stage=3)], gpus=2, fast_dev_run=True, precision=16
    )
    trainer.fit(model)

    _assert_save_model_is_equal(model, tmpdir, trainer)


@RunIf(min_gpus=1, deepspeed=True)
def test_deepspeed_skip_backward_raises(tmpdir):
    class TestModel(BoringModel):
        def training_step(self, batch, batch_idx):
            return None

    model = TestModel()
    trainer = Trainer(default_root_dir=tmpdir, plugins=[DeepSpeedPlugin()], gpus=1, fast_dev_run=True, precision=16)
    with pytest.raises(MisconfigurationException, match="returning `None` .* is not supported"):
        trainer.fit(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_warn_train_dataloader_called(tmpdir):
    """Test DeepSpeed warns when it calls ``lightning_module.train_dataloader`` internally for logging batch
    size."""
    model = BoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        plugins=[DeepSpeedPlugin()],
        gpus=1,
        fast_dev_run=True,
    )
    with pytest.warns(UserWarning, match="Inferring the batch size for internal deepspeed logging"):
        trainer.fit(model)


@RunIf(min_gpus=1, deepspeed=True, special=True)
def test_deepspeed_setup_train_dataloader(tmpdir):
    """Test DeepSpeed works when setup is required to call, and the user passes the batch size manually."""

    class TestSetupIsCalledDataModule(LightningDataModule):
        def __init__(self):
            super().__init__()
            self._setup = False

        def setup(self, stage: Optional[str] = None) -> None:
            self._setup = True

        def train_dataloader(self):
            assert self._setup
            return DataLoader(RandomDataset(32, 64), batch_size=2)

        def val_dataloader(self):
            assert self._setup
            return DataLoader(RandomDataset(32, 64), batch_size=2)

        def test_dataloader(self):
            assert self._setup
            return DataLoader(RandomDataset(32, 64), batch_size=2)

    model = BoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        plugins=[DeepSpeedPlugin(logging_batch_size_per_gpu=32)],
        gpus=1,
        fast_dev_run=True,
    )
    trainer.fit(model, datamodule=TestSetupIsCalledDataModule())
    trainer.test(model)
