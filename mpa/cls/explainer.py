# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

import os.path as osp
import numpy as np
import cv2
import torch

import mmcv
from mmcv.parallel import MMDataParallel
from mmcv.runner import load_checkpoint, wrap_fp16_model

from mmcls.datasets import build_dataloader, build_dataset
from mmcls.models import build_classifier
from mpa.modules.xai.builder import build_explainer

from mpa.registry import STAGES
from mpa.cls.stage import ClsStage
from mpa.modules.hooks.auxiliary_hooks import FeatureVectorHook, SaliencyMapHook, EigenCamHook
from mpa.modules.utils.task_adapt import prob_extractor
from mpa.utils.logger import get_logger
logger = get_logger()


@STAGES.register_module()
class ClsExplainer(ClsStage):
    def run(self, model_cfg, model_ckpt, data_cfg, **kwargs):
        """Run explain stage

        - Configuration
        - Environment setup
        - Run explain via auxiliary_hook
        """
        self._init_logger()
        mode = kwargs.get('mode', 'train')
        if mode not in self.mode:
            return {}

        cfg = self.configure(model_cfg, model_ckpt, data_cfg, training=False, **kwargs)

        mmcv.mkdir_or_exist(osp.abspath(cfg.work_dir))
        outputs = self._explain(cfg)
        return dict(
            outputs=outputs
            )

    def _explain(self, cfg):
        if cfg.get('task_adapt', False) and not hasattr(self, 'eval'):
            dataset_cfg = cfg.data.train.copy()
            dataset_cfg.pipeline = cfg.data.test.pipeline
            self.dataset = build_dataset(dataset_cfg)
        else:
            self.dataset = build_dataset(cfg.data.test)

        # Data loader
        data_loader = build_dataloader(
            self.dataset,
            samples_per_gpu=cfg.data.samples_per_gpu,
            workers_per_gpu=cfg.data.workers_per_gpu,
            dist=False,
            shuffle=False,
            round_up=False,
            persistent_workers=False)

        # build the model and load checkpoint
        model = build_classifier(cfg.model)
        self.extract_prob = hasattr(model, 'extract_prob')
        fp16_cfg = cfg.get('fp16', None)
        if fp16_cfg is not None:
            wrap_fp16_model(model)
        if cfg.load_from is not None:
            logger.info('load checkpoint from ' + cfg.load_from)
            _ = load_checkpoint(model, cfg.load_from, map_location='cpu')

        model.eval()
        model = MMDataParallel(model, device_ids=[0])

        # InferenceProgressCallback (Time Monitor enable into Infer task)
        ClsStage.set_inference_progress_callback(model, cfg)

        with FeatureVectorHook(model.module.backbone) as fhook:
            with EigenCamHook(model.module.backbone) as shook:
                # do inference and record intermediate fmap
                for data in data_loader:
                    with torch.no_grad():
                        _ = model(return_loss=False, **data)
                feature_vectors = fhook.records
                saliency_maps = shook.records

        outputs = dict(
            feature_vectors=feature_vectors,
            saliency_maps=saliency_maps
        )
        return outputs

    def _explain_blackbox(self, cfg):
        self.dataset = build_dataset(cfg.data.test)
        data_loader = build_dataloader(
            self.dataset,
            samples_per_gpu=cfg.data.samples_per_gpu,
            workers_per_gpu=cfg.data.workers_per_gpu,
            dist=False,
            shuffle=False,
            round_up=False,
            persistent_workers=False)

        # build the model and load checkpoint
        model = build_classifier(cfg.model)
        fp16_cfg = cfg.get('fp16', None)
        if fp16_cfg is not None:
            wrap_fp16_model(model)
        if cfg.load_from is not None:
            logger.info('load checkpoint from ' + cfg.load_from)
            _ = load_checkpoint(model, cfg.load_from, map_location='cpu')

        model.eval()
        model = MMDataParallel(model, device_ids=[0])

        explainer = build_explainer(model)
        explainer.eval()
        # InferenceProgressCallback (Time Monitor enable into Infer task)
        ClsStage.set_inference_progress_callback(model, cfg)
        saliency_maps = []

        with torch.no_grad():
            for data in data_loader:
                # data: [B, C, H, W]
                result = explainer(data)
                saliency_maps.append(result)

        outputs = dict(
            saliency_maps=saliency_maps
        )
        return outputs