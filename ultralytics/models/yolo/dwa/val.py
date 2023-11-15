# Ultralytics YOLO 🚀, AGPL-3.0 license

from pathlib import Path

import numpy as np
import torch

from ultralytics.models.yolo.detect import DetectionValidator
from ultralytics.utils import LOGGER, ops
from ultralytics.utils.metrics import DWAMetrics, box_iou, kpt_iou
from ultralytics.utils.plotting import output_to_target, plot_images


class DWAValidator(DetectionValidator):
    """
    A class extending the DetectionValidator class for validation based on a pose model.

    Example:
        ```python
        from ultralytics.models.yolo.pose import PoseValidator

        args = dict(model='yolov8n-pose.pt', data='coco8-pose.yaml')
        validator = PoseValidator(args=args)
        validator()
        ```
    """

    def __init__(self, dataloader=None, save_dir=None, pbar=None, args=None, _callbacks=None):
        """Initialize a 'PoseValidator' object with custom parameters and assigned attributes."""
        super().__init__(dataloader, save_dir, pbar, args, _callbacks)
        self.num_attrs = None
        self.args.task = 'dwa'
        self.metrics = DWAMetrics(save_dir=self.save_dir, on_plot=self.on_plot)
        if isinstance(self.args.device, str) and self.args.device.lower() == 'mps':
            LOGGER.warning("WARNING ⚠️ Apple MPS known Pose bug. Recommend 'device=cpu' for Pose models. "
                           'See https://github.com/ultralytics/ultralytics/issues/4031.')

    def preprocess(self, batch):
        """Preprocesses the batch by converting the 'attributes' data into a float and moving it to the device."""
        batch = super().preprocess(batch)
        batch['attributes'] = batch['attributes'].to(self.device).float()
        return batch

    #TODO
    def get_desc(self):
        """Returns description of evaluation metrics in string format."""
        return ('%22s' + '%11s' * 10) % ('Class', 'Images', 'Instances', 'Box(P', 'R', 'mAP50', 'mAP50-95)', 'Attr(Acc', 'P',
                                         'R', 'F1)')

    def postprocess(self, preds):
        """Apply non-maximum suppression and return detections with high confidence scores."""
        return ops.non_max_suppression(preds,
                                       self.args.conf,
                                       self.args.iou,
                                       labels=self.lb,
                                       multi_label=True,
                                       agnostic=self.args.single_cls,
                                       max_det=self.args.max_det,
                                       nc=self.nc)

    def init_metrics(self, model):
        """Initiate pose estimation metrics for YOLO model."""
        super().init_metrics(model)
        self.num_attrs = self.data['num_attr']

    def update_metrics(self, preds, batch):
        """Metrics."""
        for si, pred in enumerate(preds):
            idx = batch['batch_idx'] == si
            cls = batch['cls'][idx]
            bbox = batch['bboxes'][idx]
            attributes = batch['attributes'][idx]
            num_attr = attributes.shape[1]
            nl, npr = cls.shape[0], pred.shape[0]  # number of labels, predictions
            shape = batch['ori_shape'][si]
            correct_bboxes = torch.zeros(npr, self.niou, dtype=torch.bool, device=self.device)  # init
            self.seen += 1

            if npr == 0:
                if nl:
                    self.stats.append((correct_bboxes,
                        torch.zeros(0, num_attr, device=self.device), 
                        torch.zeros(0, num_attr, device=self.device),
                                        *torch.zeros(
                        (2, 0), device=self.device), cls.squeeze(-1)))
                    if self.args.plots:
                        self.confusion_matrix.process_batch(detections=None, labels=cls.squeeze(-1))
                continue

            # Predictions
            if self.args.single_cls:
                pred[:, 5] = 0
            predn = pred.clone()
            ops.scale_boxes(batch['img'][si].shape[1:], predn[:, :4], shape,
                            ratio_pad=batch['ratio_pad'][si])  # native-space pred
            pred_attr = torch.sigmoid(predn[:, 6:])

            # Evaluate
            if nl:
                height, width = batch['img'].shape[2:]
                tbox = ops.xywh2xyxy(bbox) * torch.tensor(
                    (width, height, width, height), device=self.device)  # target boxes
                ops.scale_boxes(batch['img'][si].shape[1:], tbox, shape,
                                ratio_pad=batch['ratio_pad'][si])  # native-space labels
                tattributes = attributes.clone().bool()
                labelsn = torch.cat((cls, tbox), 1)  # native-space labels
                correct_bboxes, idx = self._process_batch(predn[:, :6], labelsn)
                pred_attr= pred_attr[idx] > 0.5
                if self.args.plots:
                    self.confusion_matrix.process_batch(predn, labelsn)
                

            # Append correct_masks, correct_boxes, pconf, pcls, tcls
            self.stats.append((correct_bboxes, pred_attr, tattributes, pred[:, 4], pred[:, 5], cls.squeeze(-1)))

            # Save
            if self.args.save_json:
                self.pred_to_json(predn, batch['im_file'][si])
            # if self.args.save_txt:
            #    save_one_txt(predn, save_conf, shape, file=save_dir / 'labels' / f'{path.stem}.txt')

    def _process_batch(self, detections, labels):
        """
        Return correct prediction matrix.

        Args:
            detections (torch.Tensor): Tensor of shape [N, 6] representing detections.
                Each detection is of the format: x1, y1, x2, y2, conf, class.
            labels (torch.Tensor): Tensor of shape [M, 5] representing labels.
                Each label is of the format: class, x1, y1, x2, y2.
            pred_kpts (torch.Tensor, optional): Tensor of shape [N, 51] representing predicted attributes.
            gt_kpts (torch.Tensor, optional): Tensor of shape [N, 51] representing ground truth attributes.

        Returns:
            torch.Tensor: Correct prediction matrix of shape [N, 10] for 10 IoU levels.
        """
        
        iou = box_iou(labels[:, 1:], detections[:, :4])
        _, idx = iou.max(1)  # best iou for each label

        return self.match_predictions(detections[:, 5], labels[:, 0], iou), idx

    def plot_val_samples(self, batch, ni):
        """Plots and saves validation set samples with predicted bounding boxes and attributes."""
        plot_images(batch['img'],
                    batch['batch_idx'],
                    batch['cls'].squeeze(-1),
                    batch['bboxes'],
                    paths=batch['im_file'],
                    fname=self.save_dir / f'val_batch{ni}_labels.jpg',
                    names=self.names,
                    on_plot=self.on_plot)

    def plot_predictions(self, batch, preds, ni):
        """Plots predictions for YOLO model."""
        plot_images(batch['img'],
                    *output_to_target(preds, max_det=self.args.max_det),
                    paths=batch['im_file'],
                    fname=self.save_dir / f'val_batch{ni}_pred.jpg',
                    names=self.names,
                    on_plot=self.on_plot)  # pred

    def pred_to_json(self, predn, filename):
        """Converts YOLO predictions to COCO JSON format."""
        stem = Path(filename).stem
        image_id = int(stem) if stem.isnumeric() else stem
        box = ops.xyxy2xywh(predn[:, :4])  # xywh
        box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner
        for p, b in zip(predn.tolist(), box.tolist()):
            self.jdict.append({
                'image_id': image_id,
                'category_id': self.class_map[int(p[5])],
                'bbox': [round(x, 3) for x in b],
                'attributes': p[6:],
                'score': round(p[4], 5)})
            
    def print_results(self):
        """Prints training/validation set metrics per class."""
        pf = '%22s' + '%11i' * 2 + '%11.3g' * len(self.metrics.keys)  # print format
        pf_det = '%22s' + '%11i' * 2 + '%11.3g' * len(self.metrics.keys[:4])
        LOGGER.info(pf % ('all', self.seen, self.nt_per_class.sum(), *self.metrics.mean_results()))
        if self.nt_per_class.sum() == 0:
            LOGGER.warning(
                f'WARNING ⚠️ no labels found in {self.args.task} set, can not compute metrics without labels')

        # Print results per class
        if self.args.verbose and not self.training and self.nc > 1 and len(self.stats):
            for i, c in enumerate(self.metrics.ap_class_index):
                LOGGER.info(pf_det % (self.names[c], self.seen, self.nt_per_class[c], *self.metrics.class_result(i)))

        if self.args.plots:
            for normalize in True, False:
                self.confusion_matrix.plot(save_dir=self.save_dir,
                                           names=self.names.values(),
                                           normalize=normalize,
                                           on_plot=self.on_plot)

    #TODO: check this
    # def eval_json(self, stats):
    #     """Evaluates object detection model using COCO JSON format."""
    #     if self.args.save_json and self.is_coco and len(self.jdict):
    #         anno_json = self.data['path'] / 'annotations/person_keypoints_val2017.json'  # annotations
    #         pred_json = self.save_dir / 'predictions.json'  # predictions
    #         LOGGER.info(f'\nEvaluating pycocotools mAP using {pred_json} and {anno_json}...')
    #         try:  # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
    #             check_requirements('pycocotools>=2.0.6')
    #             from pycocotools.coco import COCO  # noqa
    #             from pycocotools.cocoeval import COCOeval  # noqa

    #             for x in anno_json, pred_json:
    #                 assert x.is_file(), f'{x} file not found'
    #             anno = COCO(str(anno_json))  # init annotations api
    #             pred = anno.loadRes(str(pred_json))  # init predictions api (must pass string, not Path)
    #             for i, eval in enumerate([COCOeval(anno, pred, 'bbox'), COCOeval(anno, pred, 'keypoints')]):
    #                 if self.is_coco:
    #                     eval.params.imgIds = [int(Path(x).stem) for x in self.dataloader.dataset.im_files]  # im to eval
    #                 eval.evaluate()
    #                 eval.accumulate()
    #                 eval.summarize()
    #                 idx = i * 4 + 2
    #                 stats[self.metrics.keys[idx + 1]], stats[
    #                     self.metrics.keys[idx]] = eval.stats[:2]  # update mAP50-95 and mAP50
    #         except Exception as e:
    #             LOGGER.warning(f'pycocotools unable to run: {e}')
    #     return stats
