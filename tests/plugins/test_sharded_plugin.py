import os
from unittest import mock

import pytest
import torch

from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import Callback
from pytorch_lightning.plugins import DDPShardedPlugin, DDPSpawnShardedPlugin
from pytorch_lightning.trainer.states import TrainerFn
from pytorch_lightning.utilities import _FAIRSCALE_AVAILABLE
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers.boring_model import BoringModel
from tests.helpers.runif import RunIf

if _FAIRSCALE_AVAILABLE:
    from fairscale.nn.data_parallel.sharded_ddp import ShardedDataParallel


@pytest.mark.parametrize("clip_val", [0, 10])
@RunIf(min_gpus=1, skip_windows=True, amp_native=True, fairscale=True)
@mock.patch("fairscale.optim.oss.OSS.clip_grad_norm")
def test_ddp_sharded_precision_16_clip_gradients(mock_oss_clip_grad_norm, clip_val, tmpdir):
    """Ensure that clip gradients is only called if the value is greater than 0."""
    model = BoringModel()
    trainer = Trainer(accelerator="ddp_sharded", gpus=1, precision=16, fast_dev_run=True, gradient_clip_val=clip_val)
    trainer.fit(model)
    if clip_val > 0:
        mock_oss_clip_grad_norm.assert_called()
    else:
        mock_oss_clip_grad_norm.assert_not_called()


@RunIf(fairscale=True)
@pytest.mark.parametrize(["accelerator"], [("ddp_sharded",), ("ddp_sharded_spawn",)])
def test_sharded_ddp_choice(tmpdir, accelerator):
    """Test to ensure that plugin is correctly chosen."""

    class CB(Callback):
        def on_fit_start(self, trainer, pl_module):
            if accelerator == "ddp_sharded":
                assert isinstance(trainer.accelerator.training_type_plugin, DDPShardedPlugin)
            elif accelerator == "ddp_sharded_spawn":
                assert isinstance(trainer.accelerator.training_type_plugin, DDPSpawnShardedPlugin)
            raise SystemExit()

    model = BoringModel()
    trainer = Trainer(fast_dev_run=True, accelerator=accelerator, callbacks=[CB()])

    with pytest.raises(SystemExit):
        trainer.fit(model)


@RunIf(amp_apex=True, fairscale=True)
def test_invalid_apex_sharded(tmpdir):
    """Test to ensure that we raise an error when we try to use apex and sharded."""

    model = BoringModel()
    with pytest.raises(MisconfigurationException, match="Sharded Plugin is not supported with Apex AMP"):
        trainer = Trainer(fast_dev_run=True, accelerator="ddp_sharded_spawn", precision=16, amp_backend="apex")

        trainer.fit(model)


@RunIf(min_gpus=2, amp_native=True, fairscale=True)
@pytest.mark.parametrize(["accelerator"], [("ddp_sharded",), ("ddp_sharded_spawn",)])
def test_ddp_choice_sharded_amp(tmpdir, accelerator):
    """Test to ensure that plugin native amp plugin is correctly chosen when using sharded."""

    class CB(Callback):
        def on_fit_start(self, trainer, pl_module):
            if accelerator == "ddp_sharded":
                assert isinstance(trainer.accelerator.training_type_plugin, DDPShardedPlugin)
            elif accelerator == "ddp_sharded_spawn":
                assert isinstance(trainer.accelerator.training_type_plugin, DDPSpawnShardedPlugin)
            raise SystemExit()

    model = BoringModel()
    trainer = Trainer(fast_dev_run=True, gpus=1, precision=16, accelerator=accelerator, callbacks=[CB()])

    with pytest.raises(SystemExit):
        trainer.fit(model)


@RunIf(skip_windows=True, fairscale=True)
def test_ddp_sharded_plugin_checkpoint_cpu(tmpdir):
    """Test to ensure that checkpoint is saved correctly."""
    model = BoringModel()
    trainer = Trainer(accelerator="ddp_sharded_spawn", num_processes=2, fast_dev_run=True)

    trainer.fit(model)

    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer.save_checkpoint(checkpoint_path)
    saved_model = BoringModel.load_from_checkpoint(checkpoint_path)

    # Assert model parameters are identical after loading
    for ddp_param, shard_param in zip(model.parameters(), saved_model.parameters()):
        assert torch.equal(ddp_param.to("cpu"), shard_param)


@RunIf(min_gpus=2, skip_windows=True, fairscale=True)
def test_ddp_sharded_plugin_checkpoint_multi_gpu(tmpdir):
    """Test to ensure that checkpoint is saved correctly when using multiple GPUs."""
    model = BoringModel()
    trainer = Trainer(gpus=2, accelerator="ddp_sharded_spawn", fast_dev_run=True)

    trainer.fit(model)

    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer.save_checkpoint(checkpoint_path)
    saved_model = BoringModel.load_from_checkpoint(checkpoint_path)

    # Assert model parameters are identical after loading
    for ddp_param, shard_param in zip(model.parameters(), saved_model.parameters()):
        assert torch.equal(ddp_param.to("cpu"), shard_param)


@RunIf(min_gpus=2, skip_windows=True, fairscale=True)
def test_ddp_sharded_plugin_finetune(tmpdir):
    """Test to ensure that we can save and restart training (simulate fine-tuning)"""
    model = BoringModel()
    trainer = Trainer(gpus=2, accelerator="ddp_sharded_spawn", fast_dev_run=True)
    trainer.fit(model)

    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer.save_checkpoint(checkpoint_path)
    saved_model = BoringModel.load_from_checkpoint(checkpoint_path)

    trainer = Trainer(fast_dev_run=True)
    trainer.fit(saved_model)


@RunIf(skip_windows=True, fairscale=True)
def test_ddp_sharded_plugin_resume_from_checkpoint(tmpdir):
    """Test to ensure that resuming from checkpoint works."""
    model = BoringModel()
    trainer = Trainer(accelerator="ddp_sharded_spawn", num_processes=2, fast_dev_run=True)

    trainer.fit(model)

    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer.save_checkpoint(checkpoint_path)

    model = BoringModel()

    trainer = Trainer(
        accelerator="ddp_sharded_spawn", num_processes=2, fast_dev_run=True, resume_from_checkpoint=checkpoint_path
    )

    trainer.fit(model)


@pytest.mark.skip(reason="Not a critical test, skip till drone CI performance improves.")  # todo
@pytest.mark.skip(reason="Currently unsupported restarting training on different number of devices.")
@RunIf(min_gpus=2, skip_windows=True, fairscale=True)
def test_ddp_sharded_plugin_resume_from_checkpoint_downsize_gpus(tmpdir):
    """Test to ensure that resuming from checkpoint works when downsizing number of GPUS."""
    model = BoringModel()
    trainer = Trainer(accelerator="ddp_sharded_spawn", fast_dev_run=True, gpus=2)

    trainer.fit(model)

    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer.save_checkpoint(checkpoint_path)

    model = BoringModel()

    trainer = Trainer(
        accelerator="ddp_sharded_spawn", fast_dev_run=True, gpus=1, resume_from_checkpoint=checkpoint_path
    )

    trainer.fit(model)


@RunIf(min_gpus=1, skip_windows=True, fairscale=True)
def test_ddp_sharded_plugin_resume_from_checkpoint_gpu_to_cpu(tmpdir):
    """Test to ensure that resuming from checkpoint works when going from GPUs- > CPU."""
    model = BoringModel()
    trainer = Trainer(accelerator="ddp_sharded_spawn", gpus=1, fast_dev_run=True)

    trainer.fit(model)

    checkpoint_path = os.path.join(tmpdir, "model.pt")
    trainer.save_checkpoint(checkpoint_path)

    model = BoringModel()

    trainer = Trainer(
        accelerator="ddp_sharded_spawn", num_processes=2, fast_dev_run=True, resume_from_checkpoint=checkpoint_path
    )

    trainer.fit(model)


@RunIf(skip_windows=True, special=True, fairscale=True)
@pytest.mark.parametrize("trainer_kwargs", (dict(num_processes=2), pytest.param(dict(gpus=2), marks=RunIf(min_gpus=2))))
def test_ddp_sharded_plugin_test_multigpu(tmpdir, trainer_kwargs):
    """Test to ensure we can use validate and test without fit."""
    model = BoringModel()
    trainer = Trainer(accelerator="ddp_sharded_spawn", fast_dev_run=True, **trainer_kwargs)

    trainer.validate(model)
    trainer.test(model)


class ManualBoringModel(BoringModel):
    def __init__(self):
        super().__init__()
        self.automatic_optimization = False

    def training_step(self, batch, batch_idx):
        opt = self.optimizers()
        opt.zero_grad()
        output = self(batch)
        loss = self.loss(batch, output)
        self.manual_backward(loss)
        opt.step()
        return {"loss": loss}


@RunIf(skip_windows=True, special=True, fairscale=True, min_gpus=2)
def test_ddp_sharded_plugin_manual_optimization_spawn(tmpdir):
    # todo (sean): this test has been split out as running both tests using parametrize causes "Address in use"
    model = ManualBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, accelerator="ddp_sharded_spawn", fast_dev_run=2, gpus=2)
    trainer.fit(model)


@RunIf(skip_windows=True, special=True, fairscale=True, min_gpus=2)
def test_ddp_sharded_plugin_manual_optimization(tmpdir):
    model = ManualBoringModel()
    trainer = Trainer(default_root_dir=tmpdir, accelerator="ddp_sharded", fast_dev_run=2, gpus=2)
    trainer.fit(model)


class BoringModelSharded(BoringModel):
    def on_train_start(self) -> None:
        """Check if trainer module is wrapped as ShardedDataParallel during training stage."""
        assert isinstance(self.trainer.model, ShardedDataParallel)

    def on_test_start(self) -> None:
        """Check if trainer module remains as LightningModule during test stage."""
        assert isinstance(self.trainer.model, LightningModule)

    def on_validation_start(self) -> None:
        """Check if trainer module remains as LightningModule during test stage."""
        if self.trainer.state.fn == TrainerFn.FITTING:
            assert isinstance(self.trainer.model, ShardedDataParallel)
        else:
            assert isinstance(self.trainer.model, LightningModule)

    def on_predict_start(self) -> None:
        """Check if trainer module remains as LightningModule during prediction stage."""
        assert isinstance(self.trainer.model, LightningModule)


@RunIf(skip_windows=True, fairscale=True)
def test_configure_ddp(tmpdir):
    """Tests with ddp sharded plugin."""
    trainer = Trainer(default_root_dir=tmpdir, accelerator="ddp_sharded", fast_dev_run=True)

    model = BoringModelSharded()

    trainer.fit(model)
    trainer.test(model, dataloaders=model.test_dataloader())
    trainer.validate(model, dataloaders=model.val_dataloader())
    trainer.predict(model, dataloaders=model.predict_dataloader())


@RunIf(skip_windows=True, fairscale=True)
@mock.patch("pytorch_lightning.plugins.DDPShardedPlugin._wrap_optimizers", autospec=True)
@pytest.mark.parametrize("cls", [DDPShardedPlugin, DDPSpawnShardedPlugin])
def test_custom_kwargs_sharded(tmpdir, cls):
    """Tests to ensure that if custom kwargs are passed, they are set correctly."""
    plugin = cls(reduce_fp16=True)

    class_name = "sharded" if isinstance(plugin, DDPShardedPlugin) else "sharded_spawn"

    with mock.patch.object(plugin, "_model", autospec=True):
        with mock.patch(
            f"pytorch_lightning.plugins.training_type.{class_name}.ShardedDataParallel", autospec=True
        ) as mock_sharded:
            plugin.configure_ddp()
    args, kwargs = mock_sharded.call_args
    assert "reduce_fp16" in kwargs
    assert kwargs["reduce_fp16"]


@RunIf(skip_windows=True, fairscale=True)
@mock.patch("pytorch_lightning.plugins.DDPShardedPlugin._wrap_optimizers", autospec=True)
@pytest.mark.parametrize(["params", "expected_buffer_size"], [(dict(), 0), (dict(reduce_buffer_size=128), 128)])
@pytest.mark.parametrize("num_nodes", [1, 2])
def test_custom_kwargs_sharded_reduce_buffer_size(tmpdir, params, expected_buffer_size, num_nodes):
    """Tests to ensure that ``reduce_buffer_size`` is correctly set based on user kwargs."""
    plugin = DDPShardedPlugin(**params)
    plugin.num_nodes = num_nodes

    with mock.patch.object(plugin, "_model", autospec=True):
        with mock.patch(
            "pytorch_lightning.plugins.training_type.sharded.ShardedDataParallel", autospec=True
        ) as mock_sharded:
            plugin.configure_ddp()
    args, kwargs = mock_sharded.call_args
    assert "reduce_buffer_size" in kwargs

    if num_nodes > 1 and len(params) == 0:
        # If user has not specified a buffer size and we're using multiple nodes, check to see if default is set
        assert kwargs["reduce_buffer_size"] == DDPShardedPlugin._REDUCE_BUFFER_SIZE_DEFAULT
    else:
        assert kwargs["reduce_buffer_size"] == expected_buffer_size
