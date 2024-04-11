import os
import time
import torch
import logging
import argparse
from collections import OrderedDict
from fvcore.common.file_io import PathManager
from fvcore.nn.precise_bn import get_bn_modules
from torch.nn.parallel import DataParallel, DistributedDataParallel
from torch.cuda.amp import autocast
from detectron2.utils import comm
from detectron2.data import transforms as T
from detectron2.utils.env import seed_all_rng
from detectron2.utils.logger import setup_logger
from detectron2.engine import hooks, SimpleTrainer
from detectron2.utils.collect_env import collect_env_info
from detectron2.utils.events import TensorboardXWriter, CommonMetricPrinter, JSONWriter, get_event_storage

from defrcn.config import CfgNode, get_cfg
from defrcn.data import *
from defrcn.modeling import build_model
from defrcn.engine.hooks import EvalHookDeFRCN
from defrcn.checkpoint import DetectionCheckpointer
from defrcn.solver import build_lr_scheduler, build_optimizer
from defrcn.evaluation import DatasetEvaluator, inference_on_dataset, print_csv_format, verify_results
from defrcn.dataloader import MetadataCatalog, build_detection_test_loader, build_detection_train_loader


__all__ = [
    "default_argument_parser",
    "default_setup",
    "DefaultPredictor",
    "DefaultTrainer",
]


def default_argument_parser():
    """
    Create a parser with some common arguments used by DeFRCN users.

    Returns:
        argparse.ArgumentParser:
    """
    parser = argparse.ArgumentParser(description="DeFRCN Training")
    parser.add_argument("--config-file", default="", metavar="FILE",
                        help="path to config file")
    parser.add_argument("--resume", action="store_true",
                        help="whether to attempt to resume")
    parser.add_argument("--eval-only", action="store_true",
                        help="evaluate last checkpoint")
    parser.add_argument("--eval-all", action="store_true",
                        help="evaluate all saved checkpoints")
    parser.add_argument("--eval-during-train", action="store_true",
                        help="evaluate during training")
    parser.add_argument("--eval-iter", type=int, default=-1,
                        help="checkpoint iteration for evaluation")
    parser.add_argument("--start-iter", type=int, default=-1,
                        help="starting iteration for evaluation")
    parser.add_argument("--end-iter", type=int, default=-1,
                        help="ending iteration for evaluation")
    parser.add_argument("--num-gpus", type=int, default=1,
                        help="number of gpus *per machine*")
    parser.add_argument("--num-machines", type=int, default=1)
    parser.add_argument("--machine-rank", type=int, default=0,
                        help="the rank of this machine")

    # PyTorch still may leave orphan processes in multi-gpu training.
    # Therefore we use a deterministic way to obtain port,
    # so that users are aware of orphan processes by seeing the port occupied.
    port = 2 ** 15 + 2 ** 14 + hash(os.getuid()) % 2 ** 14
    parser.add_argument("--dist-url", default="tcp://127.0.0.1:{}".format(port))
    parser.add_argument("--opts", default=None, nargs=argparse.REMAINDER,
                        help="Modify config options using the command-line")
    
    return parser


def default_setup(cfg, args):
    """
    Perform some basic common setups at the beginning of a job, including:

    1. Set up the DeFRCN logger
    2. Log basic information about environment, cmdline arguments, and config
    3. Backup the config to the output directory

    Args:
        cfg (CfgNode): the full config to be used
        args (argparse.NameSpace): the command line arguments to be logged
    """
    output_dir = cfg.OUTPUT_DIR
    if comm.is_main_process() and output_dir:
        PathManager.mkdirs(output_dir)

    rank = comm.get_rank()
    setup_logger(output_dir, distributed_rank=rank, name="fvcore")
    setup_logger(output_dir, distributed_rank=rank, name="defrcn")
    logger = setup_logger(output_dir, distributed_rank=rank)

    logger.info(
        "Rank of current process: {}. World size: {}".format(
            rank, comm.get_world_size()
        )
    )
    if not cfg.MUTE_HEADER:
        logger.info("Environment info:\n" + collect_env_info())

    logger.info("Command line arguments: " + str(args))
    if hasattr(args, "config_file"):
        logger.info(
            "Contents of args.config_file={}:\n{}".format(
                args.config_file,
                PathManager.open(args.config_file, "r").read(),
            )
        )

    if not cfg.MUTE_HEADER:
        logger.info("Running with full config:\n{}".format(cfg))
    if comm.is_main_process() and output_dir:
        # Note: some of our scripts may expect the existence of
        # config.yaml in output directory
        path = os.path.join(output_dir, "config.yaml")
        with PathManager.open(path, "w") as f:
            f.write(cfg.dump())
        logger.info("Full config saved to {}".format(os.path.abspath(path)))

    # make sure each worker has a different, yet deterministic seed if specified
    seed_all_rng(None if cfg.SEED < 0 else cfg.SEED + rank)

    # cudnn benchmark has large overhead. It shouldn't be used considering the small size of
    # typical validation set.
    if not (hasattr(args, "eval_only") and args.eval_only):
        torch.backends.cudnn.benchmark = cfg.CUDNN_BENCHMARK


def assign_grads(model, grads):
    """
    Similar to `assign_weights` but this time, manually assign `grads` vector to a model.
    :param model: PyTorch Model.
    :param grads: Gradient vectors.
    :return:
    """
    state_dict = model.state_dict(keep_vars=True)
    index = 0
    for param in state_dict.keys():
        # ignore batchnorm params
        if state_dict[param].grad is None:
            continue
        if 'running_mean' in param or 'running_var' in param or 'num_batches_tracked' in param:
            continue
        param_count = state_dict[param].numel()
        param_shape = state_dict[param].shape
        state_dict[param].grad = grads[index:index+param_count].view(param_shape).clone()
        index += param_count
    model.load_state_dict(state_dict)
    return model


def flatten_grads(model):
    """
    Flattens the gradients of a model (after `.backward()` call) as a single, large vector.
    :param model: PyTorch model.
    :return: 1D torch Tensor
    """
    all_grads = []
    for name, param in model.named_parameters():
        if param.grad is not None:
            all_grads.append(param.grad.view(-1))
    return torch.cat(all_grads)

def setup_base_cfg():
    cfg = get_cfg()
    cfg.merge_from_file('/home/wxq/od/DeFRCN/configs/rdd/defrcn_det_r101_base1.yaml')
    cfg.DATALOADER.NUM_WORKERS = 0
    cfg.freeze()
    return cfg


class DefaultPredictor:
    """
    Create a simple end-to-end predictor with the given config.
    The predictor takes an BGR image, resizes it to the specified resolution,
    runs the model and produces a dict of predictions.

    This predictor takes care of model loading and input preprocessing for you.
    If you'd like to do anything more fancy, please refer to its source code
    as examples to build and use the model manually.

    Attributes:
        metadata (Metadata): the metadata of the underlying dataset, obtained from
            cfg.DATASETS.TEST.

    Examples:

    .. code-block:: python

        pred = DefaultPredictor(cfg)
        outputs = pred(inputs)
    """

    def __init__(self, cfg):
        self.cfg = cfg.clone()  # cfg can be modified by model
        self.model = build_model(self.cfg)
        self.model.eval()

        # loader
        base_cfg = setup_base_cfg()
        self.novel_loader = self.build_train_loader(cfg)
        self.base_loader = self.build_train_loader(base_cfg)

        self._novel_loader_iter = iter(self.novel_loader)
        self._base_loader_iter = iter(self.base_loader)

        checkpointer = DetectionCheckpointer(self.model)
        checkpointer.load(cfg.MODEL.WEIGHTS)

        self.transform_gen = T.ResizeShortestEdge(
            [cfg.INPUT.MIN_SIZE_TEST, cfg.INPUT.MIN_SIZE_TEST],
            cfg.INPUT.MAX_SIZE_TEST,
        )

        self.input_format = cfg.INPUT.FORMAT
        assert self.input_format in ["RGB", "BGR"], self.input_format


    @torch.no_grad()
    def __call__(self, original_image):
        """
        Args:
            original_image (np.ndarray): an image of shape (H, W, C) (in BGR order).

        Returns:
            predictions (dict): the output of the model
        """
        # Apply pre-processing to image.
        if self.input_format == "RGB":
            # whether the model expects BGR inputs or RGB
            original_image = original_image[:, :, ::-1]
        height, width = original_image.shape[:2]
        image = self.transform_gen.get_transform(original_image).apply_image(
            original_image
        )
        image = torch.as_tensor(image.astype("float32").transpose(2, 0, 1))

        inputs = {"image": image, "height": height, "width": width}
        predictions = self.model([inputs])[0]
        return predictions

    @torch.no_grad()
    def get_rpn_feature(self):
        # 改一下iter方法:
        for inputs in self:
            proposal_loss = self.model.get_rpn_feauture(inputs, is_base=False)

        return None

    @classmethod
    def build_test_loader(cls, cfg, dataset_name):
        """
        Returns:
            iterable

        It now calls :func:`defrcn.data.build_detection_test_loader`.
        Overwrite it if you'd like a different data loader.
        """
        return build_detection_test_loader(cfg, dataset_name)




class DefaultTrainer(SimpleTrainer):
    """
    A trainer with default training logic. Compared to `SimpleTrainer`, it
    contains the following logic in addition:

    1. Create model, optimizer, scheduler, dataloader from the given config.
    2. Load a checkpoint or `cfg.MODEL.WEIGHTS`, if exists.
    3. Register a few common hooks.

    It is created to simplify the **standard model training workflow** and
    reduce code boilerplate for users who only need the standard training
    workflow, with standard features. It means this class makes *many
    assumptions* about your training logic that may easily become invalid in
    a new research. In fact, any assumptions beyond those made in the
    :class:`SimpleTrainer` are too much for research.

    The code of this class has been annotated about restrictive assumptions
    it mades. When they do not work for you, you're encouraged to:

    1. Overwrite methods of this class, OR:
    2. Use :class:`SimpleTrainer`, which only does minimal SGD training and
       nothing else. You can then add your own hooks if needed. OR:
    3. Write your own training loop similar to `tools/plain_train_net.py`.

    Also note that the behavior of this class, like other functions/classes in
    this file, is not stable, since it is meant to represent the "common
    default behavior".
    It is only guaranteed to work well with the standard models and training
    workflow in DeFRCN.
    To obtain more stable behavior, write your own training logic with other
    public APIs.

    Attributes:
        scheduler:
        checkpointer (DetectionCheckpointer):
        cfg (CfgNode):

    Examples:

    .. code-block:: python

        trainer = DefaultTrainer(cfg)
        trainer.resume_or_load()  # load last checkpoint or MODEL.WEIGHTS
        trainer.train()
    """

    def __init__(self, cfg):
        """
        Args:
            cfg (CfgNode):
        """
        # Assume these objects must be constructed in this order.
        model = self.build_model(cfg)
        optimizer = self.build_optimizer(cfg, model)
        data_loader = self.build_train_loader(cfg)

        # For training, wrap with DDP. But don't need this for inference.
        if comm.get_world_size() > 1:
            model = DistributedDataParallel(
                model,
                device_ids=[comm.get_local_rank()],
                broadcast_buffers=False,
                find_unused_parameters=True,
            )

        # AMP
        unsupported = "AMPTrainer does not support single-process multi-device training!"
        if isinstance(model, DistributedDataParallel):
            assert not (model.device_ids and len(model.device_ids) > 1), unsupported
        assert not isinstance(model, DataParallel), unsupported

        super().__init__(model, data_loader, optimizer)

        # base
        base_cfg = setup_base_cfg()
        self.base_loader = self.build_train_loader(base_cfg)
        self._base_loader_iter = iter(self.base_loader)

        # parameter
        # self.alpha = cfg.DYNAMIC.ALPHA
        self.beta = cfg.DYNAMIC.BETA
        # self.epsilon = cfg.DYNAMIC.EPSILON
        # self.c = cfg.DYNAMIC.C
        self.ema_lb0 = 0
        self.ema_lb = 0
        self.ema_lb_old = 0.45
        self.scheduler = self.build_lr_scheduler(cfg, optimizer)
        # self.grad_scaler = GradScaler()
        # Assume no other objects need to be checkpointed.
        # We can later make it checkpoint the stateful hooks
        self.checkpointer = DetectionCheckpointer(
            # Assume you want to save checkpoints together with logs/statistics
            model,
            cfg.OUTPUT_DIR,
            optimizer=optimizer,
            scheduler=self.scheduler,
        )
        self.start_iter = 0
        self.max_iter = cfg.SOLVER.MAX_ITER
        self.cfg = cfg

        self.register_hooks(self.build_hooks())

    def resume_or_load(self, resume=True):
        """
        If `resume==True`, and last checkpoint exists, resume from it.

        Otherwise, load a model specified by the config.

        Args:
            resume (bool): whether to do resume or not
        """
        # The checkpoint stores the training iteration that just finished, thus we start
        # at the next iteration (or iter zero if there's no checkpoint).
        self.start_iter = (
            self.checkpointer.resume_or_load(
                self.cfg.MODEL.WEIGHTS, resume=resume
            ).get("iteration", -1)
            + 1
        )

    def build_hooks(self):
        """
        Build a list of default hooks, including timing, evaluation,
        checkpointing, lr scheduling, precise BN, writing events.

        Returns:
            list[HookBase]:
        """
        cfg = self.cfg.clone()
        cfg.defrost()
        cfg.DATALOADER.NUM_WORKERS = (
            0  # save some memory and time for PreciseBN
        )

        ret = [
            hooks.IterationTimer(),
            hooks.LRScheduler(self.optimizer, self.scheduler),
            hooks.PreciseBN(
                # Run at the same freq as (but before) evaluation.
                cfg.TEST.EVAL_PERIOD,
                self.model,
                # Build a new data loader to not affect training
                self.build_train_loader(cfg),
                cfg.TEST.PRECISE_BN.NUM_ITER,
            )
            if cfg.TEST.PRECISE_BN.ENABLED and get_bn_modules(self.model)
            else None,
        ]

        # Do PreciseBN before checkpointer, because it updates the model and need to
        # be saved by checkpointer.
        # This is not always the best: if checkpointing has a different frequency,
        # some checkpoints may have more precise statistics than others.
        if comm.is_main_process():
            ret.append(
                hooks.PeriodicCheckpointer(
                    self.checkpointer, cfg.SOLVER.CHECKPOINT_PERIOD
                )
            )

        def test_and_save_results():
            self._last_eval_results = self.test(self.cfg, self.model)
            return self._last_eval_results

        # Do evaluation after checkpointer, because then if it fails,
        # we can use the saved checkpoint to debug.
        ret.append(EvalHookDeFRCN(
            cfg.TEST.EVAL_PERIOD, test_and_save_results, self.cfg))

        if comm.is_main_process():
            # run writers in the end, so that evaluation metrics are written
            ret.append(hooks.PeriodicWriter(self.build_writers()))
        return ret

    def build_writers(self):
        """
        Build a list of writers to be used. By default it contains
        writers that write metrics to the screen,
        a json file, and a tensorboard event file respectively.
        If you'd like a different list of writers, you can overwrite it in
        your trainer.

        Returns:
            list[EventWriter]: a list of :class:`EventWriter` objects.

        It is now implemented by:

        .. code-block:: python

            return [
                CommonMetricPrinter(self.max_iter),
                JSONWriter(os.path.join(self.cfg.OUTPUT_DIR, "metrics.json")),
                TensorboardXWriter(self.cfg.OUTPUT_DIR),
            ]

        """
        # Assume the default print/log frequency.
        return [
            # It may not always print what you want to see, since it prints "common" metrics only.
            CommonMetricPrinter(self.max_iter),
            JSONWriter(os.path.join(self.cfg.OUTPUT_DIR, "metrics.json")),
            TensorboardXWriter(self.cfg.OUTPUT_DIR),
        ]

    def train(self):
        """
        Run training.

        Returns:
            OrderedDict of results, if evaluation is enabled. Otherwise None.
        """
        super().train(self.start_iter, self.max_iter)
        if hasattr(self, "_last_eval_results") and comm.is_main_process():
            verify_results(self.cfg, self._last_eval_results)
            return self._last_eval_results

    @classmethod
    def build_model(cls, cfg):
        """
        Returns:
            torch.nn.Module:

        It now calls :func:`defrcn.modeling.build_model`.
        Overwrite it if you'd like a different model.
        """
        model = build_model(cfg)
        if not cfg.MUTE_HEADER:
            logger = logging.getLogger(__name__)
            logger.info("Model:\n{}".format(model))
        return model

    @classmethod
    def build_optimizer(cls, cfg, model):
        """
        Returns:
            torch.optim.Optimizer:

        It now calls :func:`defrcn.solver.build_optimizer`.
        Overwrite it if you'd like a different optimizer.
        """
        return build_optimizer(cfg, model)

    @classmethod
    def build_lr_scheduler(cls, cfg, optimizer):
        """
        It now calls :func:`defrcn.solver.build_lr_scheduler`.
        Overwrite it if you'd like a different scheduler.
        """
        return build_lr_scheduler(cfg, optimizer)

    @classmethod
    def build_train_loader(cls, cfg):
        """
        Returns:
            iterable

        It now calls :func:`defrcn.data.build_detection_train_loader`.
        Overwrite it if you'd like a different data loader.
        """
        return build_detection_train_loader(cfg)

    @classmethod
    def build_test_loader(cls, cfg, dataset_name):
        """
        Returns:
            iterable

        It now calls :func:`defrcn.data.build_detection_test_loader`.
        Overwrite it if you'd like a different data loader.
        """
        return build_detection_test_loader(cfg, dataset_name)

    @classmethod
    def build_evaluator(cls, cfg, dataset_name):
        """
        Returns:
            DatasetEvaluator

        It is not implemented by default.
        """
        raise NotImplementedError(
            "Please either implement `build_evaluator()` in subclasses, or pass "
            "your evaluator as arguments to `DefaultTrainer.test()`."
        )

    @classmethod
    def test(cls, cfg, model, evaluators=None):
        """
        Args:
            cfg (CfgNode):
            model (nn.Module):
            evaluators (list[DatasetEvaluator] or None): if None, will call
                :meth:`build_evaluator`. Otherwise, must have the same length as
                `cfg.DATASETS.TEST`.

        Returns:
            dict: a dict of result metrics
        """
        logger = logging.getLogger(__name__)

        if isinstance(evaluators, DatasetEvaluator):
            evaluators = [evaluators]
        if evaluators is not None:
            assert len(cfg.DATASETS.TEST) == len(
                evaluators
            ), "{} != {}".format(len(cfg.DATASETS.TEST), len(evaluators))

        results = OrderedDict()
        for idx, dataset_name in enumerate(cfg.DATASETS.TEST):
            data_loader = cls.build_test_loader(cfg, dataset_name)
            # When evaluators are passed in as arguments,
            # implicitly assume that evaluators can be created before data_loader.
            if evaluators is not None:
                evaluator = evaluators[idx]
            else:
                try:
                    evaluator = cls.build_evaluator(cfg, dataset_name)
                except NotImplementedError:
                    logger.warn(
                        "No evaluator found. Use `DefaultTrainer.test(evaluators=)`, "
                        "or implement its `build_evaluator` method."
                    )
                    results[dataset_name] = {}
                    continue
            results_i = inference_on_dataset(model, data_loader, evaluator, cfg)
            results[dataset_name] = results_i
            if comm.is_main_process():
                assert isinstance(
                    results_i, dict
                ), "Evaluator must return a dict on the main process. Got {} instead.".format(
                    results_i
                )
                logger.info(
                    "Evaluation results for {} in csv format:".format(
                        dataset_name
                    )
                )
                print_csv_format(results_i)

        if len(results) == 1:
            results = list(results.values())[0]
        return results

    def run_step(self):
        """
        Implement the standard training logic described above.
        """
        if self.iter > 8000:
            return
        assert self.model.training, "[SimpleTrainer] model was changed to eval mode!"
        storage = get_event_storage()
        self.optimizer.zero_grad()

        # novel
        data = next(self._data_loader_iter)
        try:
            loss_dict = self.model(data, is_base=False)
        except:
            return
        losses_novel = sum(loss_dict.values())  # 两种loss加起来算
        losses_novel.backward()
        storage.put_scalar("dynamic/loss_n", losses_novel)
        grad_novel = flatten_grads(self.model).detach().clone()

        self.optimizer.zero_grad()

        # base
        data_base = next(self._base_loader_iter)
        loss_dict = self.model(data_base, is_base=True)
        losses_base = sum(loss_dict.values())
        losses_base.backward()
        if self.iter == 0:
            self.ema_lb0 = losses_base
            self.ema_lb = losses_base
        storage.put_scalar("dynamic/loss_b", losses_base)
        grad_base = flatten_grads(self.model).detach().clone()

        with torch.no_grad():
            # self.ema_lb = self.beta * self.ema_lb + (1 - self.beta) * losses_base
            self.ema_lb_old = self.beta * self.ema_lb_old + (1 - self.beta) * losses_base
            general_converge = 3 * (self.ema_lb_old - 0.2) / self.ema_lb_old
            # general_converge = (1 - math.tanh(self.ema_lb - 0.2) / math.tanh(self.ema_lb0 - 0.2)) ** 2
            # general_converge = 1
            # angle_novel = torch.dot(grad_base, grad_novel) / torch.square(torch.norm(grad_base))
            angle_base = 1 - torch.dot(grad_base, grad_novel) / torch.square(torch.norm(grad_base))
            # grad_novel = grad_novel - grad_base * angle_novel
            dynamic_lambda = max(general_converge, angle_base)
            # grad_novel = general_converge * grad_novel
            # grad_novel = dynamic_lambda * grad_novel
            # if torch.dot(grad_base, grad_novel) < 0:
            # print("here!")
            # grad_novel = 0
            # else:
            # grad_novel = grad_novel * general_converge
            # grad_base = grad_base * general_converge
            # if random.random() <= 0.15:
            #     dynamic = 0
            # else:
            #     dynamic = general_converge
            # dynamic_lambda = max(general_converge, angle)
            # dynamic_lambda = max(1, angle)
            # dynamic_lambda = angle
            grad_base = dynamic_lambda * grad_base
        # # storage.put_scalar("dynamic/lambda", dynamic_lambda)

        # storage.put_scalar("dynamic/ema_lb", self.ema_lb)
        # storage.put_scalar("dynamic/ema_lb_old", self.ema_lb_old)
        # storage.put_scalar("dynamic/ratio", ratio)
        # storage.put_scalar("dynamic/1/ratio", 1 / ratio)
        # storage.put_scalar("dynamic/dynamic", dynamic)
        # print("[l_n]:%.4f [l_b]:%.4f [ema_b]:%.4f" % (losses_novel, losses_base, self.ema_lb))
        # if dynamic == 0:
        #     print("[l_n]:%.4f [l_b]:%.4f [ema_b]:%.4f [ratio]:%.4f [1/ratio]:+inf" %
        #           (losses_novel, losses_base, self.ema_lb, dynamic))
        # else:
        #     print("[l_n]:%.4f [l_b]:%.4f [ema_b]:%.4f [ratio]:%.4f [1/ratio]:%.4f" %
        #           (losses_novel, losses_base, self.ema_lb, dynamic, 1/dynamic))
        new_grad = grad_base + grad_novel
        try:
            self.model = assign_grads(self.model, new_grad)
            self.optimizer.step()
        except:
            print("eoor")
