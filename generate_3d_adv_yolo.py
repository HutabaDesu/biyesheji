import pdb
import json
import torch
import argparse
import copy
from torchvision import models as model_load
from ultralytics import YOLO

from nerf.utils_adv_yolo import *
from nerf.network_adv import NeRFNetwork
from nerf.provider_adv import NeRFDataset as data1
from nerf.provider_adv_yolo import NeRFDataset as data2


class NormalizeByChannelMeanStd(torch.nn.Module):
    def __init__(self, mean, std):
        super(NormalizeByChannelMeanStd, self).__init__()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.mean = torch.tensor(mean).view(-1, 1, 1).to(self.device)
        self.std = torch.tensor(std).view(-1, 1, 1).to(self.device)

    def forward(self, x):
        return (x - self.mean) / self.std


def get_surrogate_model(model_name):
    resnet101 = model_load.resnet101(pretrained=True)
    densenet121 = model_load.densenet121(pretrained=True)
    yolov8 = YOLO("v8s_55e_last.pt").model
    #print(yolov8)
    networks = {
        'resnet': resnet101,
        'densenet': densenet121,
        'yolov8': yolov8,
    }
    if model_name == 'yolov8':
        resize_transform = transforms.Resize((512, 512))
        return nn.Sequential(resize_transform, networks[model_name].eval()).to(device)
    else:
        resize_transform = transforms.Resize((224, 224))
        mean = [0.485, 0.456, 0.406]
        std = [0.229, 0.224, 0.225]
        normalize = NormalizeByChannelMeanStd(mean, std)
        return nn.Sequential(resize_transform, normalize, networks[model_name].eval()).to(device)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('path', type=str)
    parser.add_argument('-O', action='store_true', help="recommended settings")
    parser.add_argument('--workspace', type=str, default='workspace')
    parser.add_argument('--target_label', type=str, default='random',
                        help="Specific target label or 'random' for random selection between 0 and 999.")
    parser.add_argument('--surrogate_model', type=str, default='resnet',
                        help="Type of surrogate model to use, default is 'resnet'.")
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--stage', type=int, default=0, help="training stage")
    parser.add_argument('--ckpt', type=str, default='latest')
    parser.add_argument('--fp16', action='store_true', help="use amp mixed precision training")
    parser.add_argument('--sdf', action='store_true', help="use sdf instead of density for nerf")
    parser.add_argument('--tcnn', action='store_true', help="use tcnn's gridencoder")
    parser.add_argument('--progressive_level', action='store_true', help="progressively increase max_level")

    ### testing options
    parser.add_argument('--test', action='store_true', help="test mode")
    parser.add_argument('--test_no_video', action='store_true', help="test mode: do not save video")
    parser.add_argument('--test_no_mesh', action='store_true', help="test mode: do not save mesh")
    parser.add_argument('--camera_traj', type=str, default='',
                        help="nerfstudio compatible json file for camera trajactory")

    ### dataset options
    parser.add_argument('--data_format', type=str, default='nerf', choices=['nerf', 'colmap', 'dtu'])
    parser.add_argument('--train_split', type=str, default='train', choices=['train', 'trainval', 'all'])
    parser.add_argument('--preload', action='store_true',
                        help="preload all data into GPU, accelerate training but use more GPU memory")
    parser.add_argument('--random_image_batch', action='store_true',
                        help="randomly sample rays from all images per step in training stage 0, incompatible with enable_sparse_depth")
    parser.add_argument('--downscale', type=int, default=2, help="downscale training images")
    parser.add_argument('--bound', type=float, default=2,
                        help="assume the scene is bounded in box[-bound, bound]^3, if > 1, will invoke adaptive ray marching.")
    parser.add_argument('--scale', type=float, default=-1,
                        help="scale camera location into box[-bound, bound]^3, -1 means automatically determine based on camera poses..")
    parser.add_argument('--offset', type=float, nargs='*', default=[0, 0, 0], help="offset of camera location")
    parser.add_argument('--mesh', type=str, default='', help="template mesh for phase 2")
    parser.add_argument('--enable_cam_near_far', action='store_true',
                        help="colmap mode: use the sparse points to estimate camera near far per view.")
    parser.add_argument('--enable_cam_center', action='store_true',
                        help="use camera center instead of sparse point center (colmap dataset only)")
    parser.add_argument('--min_near', type=float, default=0.05, help="minimum near distance for camera")
    parser.add_argument('--enable_sparse_depth', action='store_true',
                        help="use sparse depth from colmap pts3d, only valid if using --data_formt colmap")
    parser.add_argument('--enable_dense_depth', action='store_true',
                        help="use dense depth from omnidepth calibrated to colmap pts3d, only valid if using --data_formt colmap")

    ### training options
    parser.add_argument('--iters', type=int, default=20000, help="training iters")
    parser.add_argument('--lr', type=float, default=1e-2, help="initial learning rate")
    parser.add_argument('--lr_vert', type=float, default=1e-4, help="initial learning rate for vert optimization")
    parser.add_argument('--pos_gradient_boost', type=float, default=1, help="nvdiffrast option")
    parser.add_argument('--cuda_ray', action='store_true', help="use CUDA raymarching instead of pytorch")
    parser.add_argument('--max_steps', type=int, default=1024,
                        help="max num steps sampled per ray (only valid when using --cuda_ray)")
    parser.add_argument('--update_extra_interval', type=int, default=16,
                        help="iter interval to update extra status (only valid when using --cuda_ray)")
    parser.add_argument('--max_ray_batch', type=int, default=4096,
                        help="batch size of rays at inference to avoid OOM (only valid when NOT using --cuda_ray)")
    parser.add_argument('--grid_size', type=int, default=128, help="density grid resolution")
    parser.add_argument('--mark_untrained', action='store_true', help="mark_untrained grid")
    parser.add_argument('--dt_gamma', type=float, default=1 / 256,
                        help="dt_gamma (>=0) for adaptive ray marching. set to 0 to disable, >0 to accelerate rendering (but usually with worse quality)")
    parser.add_argument('--density_thresh', type=float, default=10, help="threshold for density grid to be occupied")
    parser.add_argument('--diffuse_step', type=int, default=1000,
                        help="training iters that only trains diffuse color for better initialization")
    parser.add_argument('--diffuse_only', action='store_true',
                        help="only train diffuse color by overriding --diffuse_step")
    parser.add_argument('--background', type=str, default='white', choices=['white', 'random'],
                        help="training background mode")
    parser.add_argument('--enable_offset_nerf_grad', action='store_true',
                        help="allow grad to pass through nerf to train vertices offsets in stage 1, only work for small meshes (e.g., synthetic dataset)")
    parser.add_argument('--n_eval', type=int, default=5, help="eval $ times during training")
    parser.add_argument('--n_ckpt', type=int, default=50, help="save $ times during training")

    # batch size related
    parser.add_argument('--num_rays', type=int, default=4096, help="num rays sampled per image for each training step")
    parser.add_argument('--adaptive_num_rays', action='store_true',
                        help="adaptive num rays for more efficient training")
    parser.add_argument('--num_points', type=int, default=2 ** 18,
                        help="target num points for each training step, only work with adaptive num_rays")

    # stage 0 regularizations
    parser.add_argument('--lambda_density', type=float, default=0, help="loss scale")
    parser.add_argument('--lambda_entropy', type=float, default=0, help="loss scale")
    parser.add_argument('--lambda_tv', type=float, default=1e-8, help="loss scale")
    parser.add_argument('--lambda_depth', type=float, default=0.1, help="loss scale")
    parser.add_argument('--lambda_specular', type=float, default=1e-5, help="loss scale")
    parser.add_argument('--lambda_eikonal', type=float, default=0.1, help="loss scale")
    parser.add_argument('--lambda_rgb', type=float, default=1, help="loss scale")
    parser.add_argument('--lambda_mask', type=float, default=0.1, help="loss scale")

    # stage 1 regularizations
    parser.add_argument('--wo_smooth', action='store_true', help="disable all smoothness regularizations")
    parser.add_argument('--lambda_lpips', type=float, default=0, help="loss scale")
    parser.add_argument('--lambda_offsets', type=float, default=0.1, help="loss scale")
    parser.add_argument('--lambda_lap', type=float, default=0.001, help="loss scale")
    parser.add_argument('--lambda_normal', type=float, default=0, help="loss scale")
    parser.add_argument('--lambda_edgelen', type=float, default=0, help="loss scale")
    parser.add_argument('--lambda_cd', type=float, default=3000, help="loss scale")

    # unused
    parser.add_argument('--contract', action='store_true',
                        help="apply L-INF ray contraction as in mip-nerf, only work for bound > 1, will override bound to 2.")
    parser.add_argument('--patch_size', type=int, default=1,
                        help="[experimental] render patches in training, so as to apply LPIPS loss. 1 means disabled, use [64, 32, 16] to enable")
    parser.add_argument('--trainable_density_grid', action='store_true',
                        help="update density_grid through loss functions, instead of directly update.")
    parser.add_argument('--color_space', type=str, default='srgb', help="Color space, supports (linear, srgb)")
    parser.add_argument('--ind_dim', type=int, default=0, help="individual code dim, 0 to turn off")
    parser.add_argument('--ind_num', type=int, default=500,
                        help="number of individual codes, should be larger than training dataset size")

    ### mesh options
    # stage 0
    parser.add_argument('--mcubes_reso', type=int, default=512, help="resolution for marching cubes")
    parser.add_argument('--env_reso', type=int, default=256, help="max layers (resolution) for env mesh")
    parser.add_argument('--decimate_target', type=float, default=3e5,
                        help="decimate target for number of triangles, <=0 to disable")
    parser.add_argument('--mesh_visibility_culling', action='store_true',
                        help="cull mesh faces based on visibility in training dataset")
    parser.add_argument('--visibility_mask_dilation', type=int, default=5, help="visibility dilation")
    parser.add_argument('--clean_min_f', type=int, default=8, help="mesh clean: min face count for isolated mesh")
    parser.add_argument('--clean_min_d', type=int, default=5, help="mesh clean: min diameter for isolated mesh")

    # stage 1
    parser.add_argument('--ssaa', type=int, default=2, help="super sampling anti-aliasing ratio")
    parser.add_argument('--texture_size', type=int, default=1024, help="exported texture resolution")
    parser.add_argument('--refine', action='store_true', help="track face error and do subdivision")
    parser.add_argument("--refine_steps_ratio", type=float, action="append", default=[0.1, 0.2, 0.3, 0.4, 0.5, 0.7])
    parser.add_argument('--refine_size', type=float, default=0.01, help="refine trig length")
    parser.add_argument('--refine_decimate_ratio', type=float, default=0.1, help="refine decimate ratio")
    parser.add_argument('--refine_remesh_size', type=float, default=0.02, help="remesh trig length")

    ### GUI options
    parser.add_argument('--vis_pose', action='store_true', help="visualize the poses")
    parser.add_argument('--gui', action='store_true', help="start a GUI")
    parser.add_argument('--W', type=int, default=1000, help="GUI width")
    parser.add_argument('--H', type=int, default=1000, help="GUI height")
    parser.add_argument('--radius', type=float, default=5, help="default GUI camera radius from center")
    parser.add_argument('--fovy', type=float, default=50, help="default GUI camera fovy")
    parser.add_argument('--max_spp', type=int, default=1, help="GUI rendering max sample per pixel")
    
    parser.add_argument('--back_file', type=str, help="background images path")
    parser.add_argument('--nc', type=int, default=80, help="num of classes")
    parser.add_argument('--task', type=int, default=1, help="the task of optimization")
    
    opt = parser.parse_args()

    opt.cuda_ray = True

    if opt.O:
        opt.fp16 = True
        opt.preload = True
        opt.mark_untrained = True
        opt.random_image_batch = True
        opt.mesh_visibility_culling = True
        opt.adaptive_num_rays = True
        opt.refine = True

    if opt.sdf:
        opt.density_thresh = 0.001  # use smaller thresh to suit density scale from sdf
        if opt.stage == 0:
            opt.progressive_level = True

        # contract background
        if opt.bound > 1:
            opt.contract = True

        opt.enable_offset_nerf_grad = True  # lead to more sharp texture

        # just perform remesh periodically
        opt.refine_decimate_ratio = 0  # disable decimationlambda
        opt.refine_size = 0  # disable subdivision

    if opt.contract:
        # mark untrained is not very correct in contraction mode...
        opt.mark_untrained = False

    # best rendering quality at the sacrifice of mesh quality
    if opt.wo_smooth:
        opt.lambda_offsets = 0
        opt.lambda_lap = 0
        opt.lambda_normal = 0

    if opt.enable_sparse_depth:
        print(f'[WARN] disable random image batch when depth supervision is used!')
        opt.random_image_batch = False

    if opt.patch_size > 1:
        # assert opt.patch_size > 16, "patch_size should > 16 to run LPIPS loss."
        assert opt.num_rays % (opt.patch_size ** 2) == 0, "patch_size ** 2 should be dividable by num_rays."

    # convert ratio to steps
    opt.refine_steps = [int(round(x * opt.iters)) for x in opt.refine_steps_ratio]
    
    # seed_everything(opt.seed)
    model = NeRFNetwork(opt)
    model2 = NeRFNetwork(opt).eval()
    
    criterion = torch.nn.MSELoss(reduction='none')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    optimizer = lambda model: torch.optim.Adam(model.get_params(opt.lr), eps=1e-15)
    print('The number of params: {}'.format(len((model.get_params(opt.lr)))))
    
    if opt.task == 1:
        train_loader = data1(opt, device=device, type=opt.train_split).dataloader()
    elif opt.task == 2:
        train_loader = data2(opt, device=device, type=opt.train_split).dataloader()
        
    max_epoch = np.ceil(opt.iters / len(train_loader)).astype(np.int32)
    save_interval = max(1, max_epoch // max(opt.n_ckpt, 1))
    eval_interval = max(1, max_epoch // max(opt.n_eval, 1))
    print(f'[INFO] max_epoch {max_epoch}, eval every {eval_interval}, save every {save_interval}.')

    if opt.ind_dim > 0:
        assert len(
            train_loader) < opt.ind_num, f"[ERROR] dataset too many frames: {len(train_loader)}, please increase --ind_num to at least this number!"
    scheduler = lambda optimizer: optim.lr_scheduler.LambdaLR(optimizer, lambda iter: 0.01 + 0.99 * (
                iter / 500) if iter <= 500 else 1 * (0.1 ** ((iter - 500) / (opt.iters - 500))))

    surrogate_model = get_surrogate_model(opt.surrogate_model)
    with open('dataset/imagenet-simple-labels.json', 'r') as file:
        imagenet_labels = json.load(file)
    if opt.target_label.lower() == 'random':
        target = random.randint(0, 999)
    else:
        try:
            target = int(opt.target_label)
            if target < 1 or target > 999:
                raise ValueError("Target label must be between 0 and 999.")
        except ValueError:
            print("Invalid target_label. It must be 'random' or an integer between 0 and 999.")
            exit(1)
    print(f'We are attacking object towards {target}:{imagenet_labels[target]}')

    trainer = Trainer('ngp', opt, surrogate_model, model, model2, target, device=device, workspace=opt.workspace,
                      optimizer=optimizer, criterion=criterion, ema_decay=0.95 if opt.stage == 0 else None,
                      fp16=opt.fp16, \
                      lr_scheduler=scheduler, scheduler_update_every_step=True, use_checkpoint=opt.ckpt,
                      eval_interval=eval_interval, save_interval=save_interval)
    if opt.task == 1:
        valid_loader = data1(opt, device=device, type='val').dataloader()
    elif opt.task == 2:
        valid_loader = data2(opt, device=device, type='val').dataloader()
        
    trainer.metrics = [PSNRMeter(), ]
    trainer.train(train_loader, valid_loader, max_epoch)

    # last validation
    trainer.metrics = [PSNRMeter(), SSIMMeter(), LPIPSMeter(device=device)]
    trainer.evaluate(valid_loader)

    # also test
    if opt.task == 1:
        test_loader = data1(opt, device=device, type='test').dataloader()
    elif opt.task == 2:    
        test_loader = data2(opt, device=device, type='test').dataloader()
        
    if test_loader.has_gt:
        trainer.evaluate(test_loader)  # blender has gt, so evaluate it.
    trainer.test(test_loader, write_video=True)  # test and save video

    if opt.stage == 1:
        trainer.export_stage1(resolution=opt.texture_size)
    else:
        trainer.save_mesh(resolution=opt.mcubes_reso, decimate_target=opt.decimate_target,
                          dataset=train_loader._data if opt.mesh_visibility_culling else None)
