# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""Project given image to the latent space of pretrained network pickle."""

import copy
import os
from time import perf_counter

import click
import cv2
import numpy as np
import PIL.Image
import torch
import torch.nn.functional as F

import dnnlib
import legacy

# ----------------------------------------------------------------------

import torch
import torch.nn as nn
import numpy as np
from tqdm import tqdm

'''
class UniversalPerturbation:
    def __init__(self, model, epsilon=0.25, delta=0.2, max_iter_uni=50, max_iter_df=10):
        self.model = model
        self.epsilon = epsilon  # 최대 섭동 크기
        self.delta = delta  # 원하는 성공률
        self.max_iter_uni = max_iter_uni
        self.max_iter_df = max_iter_df

    def generate(self, images, labels):
        n = len(images)
        v = torch.zeros_like(images[0])  # 유니버설 섭동 초기화

        fooling_rate = 0.0
        itr = 0

        while fooling_rate < 1 - self.delta and itr < self.max_iter_uni:
            np.random.shuffle(images)

            for image, label in tqdm(zip(images, labels), total=n):
                perturbed = image + v

                # 현재 이미지가 이미 잘못 분류되는지 확인
                pred = torch.argmax(self.model(perturbed.unsqueeze(0)), dim=1)
                if pred == label:
                    # DeepFool로 추가 섭동 계산
                    df = DeepFool(self.model, self.max_iter_df)
                    delta_v = df.generate(perturbed.unsqueeze(0), label)

                    if delta_v is not None:
                        v = v + delta_v.squeeze(0)
                        # 섭동 크기 제한
                        v = torch.clamp(v, -self.epsilon, self.epsilon)

            # 성공률 계산
            fooling_rate = self.compute_fooling_rate(images, labels, v)
            itr += 1

        return v

    def compute_fooling_rate(self, images, labels, v):
        n = len(images)
        fooled = 0

        for image, label in zip(images, labels):
            perturbed = image + v
            pred = torch.argmax(self.model(perturbed.unsqueeze(0)), dim=1)
            if pred != label:
                fooled += 1

        return fooled / n

class AdversarialAttack:

def apply_attack(model, image, target_label, attack_type="universal"):
    if attack_type == "universal":
        attack = UniversalPerturbation(model)
        perturbation = attack.generate([image], [target_label])
    else:  # deepfool
        attack = DeepFool(model)
        perturbation = attack.generate(image, target_label)

    return perturbation

'''
# -------------------------------------------------------------------
class DeepFoolAttack:
    def __init__(self, model, max_iter=10, epsilon=0.1):
        self.model = model
        self.max_iter = max_iter
        self.epsilon = epsilon
    
    def generate_perturbation(self, images, target_features):
        self.model.eval()
        images = images.clone().detach().requires_grad_(True)
        
        # Normalize images for VGG16
        normalized_images = images * (1/255.0)
        
        best_perturbation = None
        min_perturbation_norm = float('inf')
        
        # Get original features
        with torch.enable_grad():
            original_features = self.model(normalized_images, resize_images=False, return_lpips=True)
            
            for _ in range(self.max_iter):
                # Calculate gradients w.r.t current features
                if images.grad is not None:
                    images.grad.data.zero_()
                
                current_features = self.model(normalized_images, resize_images=False, return_lpips=True)
                loss = (current_features - target_features).square().sum()
                loss.backward(retain_graph=True)
                
                # Get gradient
                grad = images.grad.data
                
                # Calculate perturbation
                perturbation = self.epsilon * grad.sign()
                perturbation_norm = torch.norm(perturbation)
                
                # Update best perturbation if this one is smaller
                if perturbation_norm < min_perturbation_norm:
                    min_perturbation_norm = perturbation_norm
                    best_perturbation = perturbation.clone()
                
                # Apply perturbation
                with torch.no_grad():
                    normalized_images = normalized_images + perturbation
                    normalized_images = torch.clamp(normalized_images, 0, 1)
                
                # Check if we've successfully changed the features significantly
                new_features = self.model(normalized_images, resize_images=False, return_lpips=True)
                if torch.norm(new_features - original_features) > self.epsilon:
                    break
        
        return best_perturbation * 255.0  # Scale back to image range

    def __init__(self, model, epsilon=0.1, max_iter=10):
        self.model = model
        self.epsilon = epsilon
        self.max_iter = max_iter
        
    def generate_perturbation(self, images, target_features):
        images = images.clone().detach().requires_grad_(True)
        
        # VGG 입력을 위한 이미지 정규화 (in-place 연산 제거)
        normalized_images = images * (1/255.0)  # in-place 연산(/) 대신 곱셈 사용
        
        # VGG 특징 추출
        image_features = self.model(normalized_images, resize_images=False, return_lpips=True)
        
        # 손실 계산
        loss = (target_features - image_features).square().sum()
        
        # 그래디언트 계산
        loss.backward()
        
        # FGSM 스타일의 섭동 생성
        with torch.no_grad():
            perturbation = self.epsilon * images.grad.sign()
            
        return perturbation
# --------------------------------------------------------------

def project_with_adversarial(
    G,
    target: torch.Tensor,
    *,
    num_steps                  = 1000,
    w_avg_samples              = 10000,
    initial_learning_rate      = 0.01,
    initial_noise_factor       = 0.05,
    lr_rampdown_length         = 0.25,
    lr_rampup_length          = 0.1,
    noise_ramp_length         = 0.75,
    regularize_noise_weight   = 1e5,
    adversarial_weight        = 0.3,
    verbose                   = False,
    device: torch.device
):
    assert target.shape == (G.img_channels, G.img_resolution, G.img_resolution)

    def logprint(*args):
        if verbose:
            print(*args)

    G = copy.deepcopy(G).eval().requires_grad_(False).to(device)

    # Compute w stats
    logprint(f'Computing W midpoint and stddev using {w_avg_samples} samples...')
    z_samples = np.random.RandomState(123).randn(w_avg_samples, G.z_dim)
    w_samples = G.mapping(torch.from_numpy(z_samples).to(device), None)
    w_samples = w_samples[:, :1, :].cpu().numpy().astype(np.float32)
    w_avg = np.mean(w_samples, axis=0, keepdims=True)
    w_std = (np.sum((w_samples - w_avg) ** 2) / w_avg_samples) ** 0.5

    # Setup noise inputs
    noise_bufs = { name: buf for (name, buf) in G.synthesis.named_buffers() if 'noise_const' in name }

    # Load VGG16 feature detector
    url = 'https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/metrics/vgg16.pt'
    with dnnlib.util.open_url(url) as f:
        vgg16 = torch.jit.load(f).eval().to(device)

    # Initialize DeepFool attack
    attack = DeepFoolAttack(vgg16, max_iter=10, epsilon=0.1)

    # Features for target image
    target_images = target.unsqueeze(0).to(device).to(torch.float32)
    if target_images.shape[2] > 256:
        target_images = F.interpolate(target_images, size=(256, 256), mode='area')
    target_features = vgg16(target_images, resize_images=False, return_lpips=True)

    w_opt = torch.tensor(w_avg, dtype=torch.float32, device=device, requires_grad=True)
    w_out = torch.zeros([num_steps] + list(w_opt.shape[1:]), dtype=torch.float32, device=device)
    optimizer = torch.optim.Adam([w_opt] + list(noise_bufs.values()), betas=(0.9, 0.999), lr=initial_learning_rate)

    # Init noise
    for buf in noise_bufs.values():
        buf[:] = torch.randn_like(buf)
        buf.requires_grad = True

    for step in range(num_steps):
        # Learning rate schedule
        t = step / num_steps
        w_noise_scale = w_std * initial_noise_factor * max(0.0, 1.0 - t / noise_ramp_length) ** 2
        lr_ramp = min(1.0, (1.0 - t) / lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        lr_ramp = lr_ramp * min(1.0, t / lr_rampup_length)
        lr = initial_learning_rate * lr_ramp
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        # Synth images from opt_w
        w_noise = torch.randn_like(w_opt) * w_noise_scale
        ws = (w_opt + w_noise).repeat([1, G.mapping.num_ws, 1])
        synth_images = G.synthesis(ws, noise_mode='const')

        # Prepare images for VGG
        synth_images = (synth_images + 1) * (255/2)
        if synth_images.shape[2] > 256:
            synth_images = F.interpolate(synth_images, size=(256, 256), mode='area')

        # Apply DeepFool perturbation every 10 steps
        if step % 10 == 0:
            perturbation = attack.generate_perturbation(synth_images, target_features)
            if perturbation is not None:
                synth_images = synth_images + adversarial_weight * perturbation

        # Features for synth images
        synth_features = vgg16(synth_images, resize_images=False, return_lpips=True)
        dist = (target_features - synth_features).square().sum()

        # Noise regularization
        reg_loss = 0.0
        for v in noise_bufs.values():
            noise = v[None,None,:,:] 
            while True:
                reg_loss += (noise*torch.roll(noise, shifts=1, dims=3)).mean()**2
                reg_loss += (noise*torch.roll(noise, shifts=1, dims=2)).mean()**2
                if noise.shape[2] <= 8:
                    break
                noise = F.avg_pool2d(noise, kernel_size=2)
        
        loss = dist + reg_loss * regularize_noise_weight

        # Step
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
        logprint(f'step {step+1:>4d}/{num_steps}: dist {dist:<4.2f} loss {float(loss):<5.2f}')

        # Save projected W
        w_out[step] = w_opt.detach()[0]

        # Normalize noise
        with torch.no_grad():
            for buf in noise_bufs.values():
                buf -= buf.mean()
                buf *= buf.square().mean().rsqrt()

    return w_out.repeat([1, G.mapping.num_ws, 1])

# ----------------------------------------------------------------------------

@click.command()
@click.option('--network', 'network_pkl', help='Network pickle filename', required=True)
@click.option('--target', 'target_fname', help='Target image file to project to', required=True, metavar='FILE')
@click.option('--num-steps',              help='Number of optimization steps', type=int, default=1000, show_default=True)
@click.option('--seed',                   help='Random seed', type=int, default=300, show_default=True)
@click.option('--save-video',             help='Save an mp4 video of optimization progress', type=bool, default=True, show_default=True)
@click.option('--outdir',                 help='Where to save the output images', required=True, metavar='DIR')
def run_projection(
    network_pkl: str,
    target_fname: str,
    outdir: str,
    save_video: bool,
    seed: int,
    num_steps: int
):
    """Project given image to the latent space of pretrained network pickle.

    Examples:

    \b
    python projector.py --outdir=out --target=~/mytargetimg.png \\
        --network=https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/ffhq.pkl
    """
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Load networks.
    print('Loading networks from "%s"...' % network_pkl)
    device = torch.device('cuda')
    with dnnlib.util.open_url(network_pkl) as fp:
        G = legacy.load_network_pkl(fp)['G_ema'].requires_grad_(False).to(device) # type: ignore

    # Load target image.
    target_pil = PIL.Image.open(target_fname).convert('RGB')
    w, h = target_pil.size
    s = min(w, h)
    target_pil = target_pil.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    target_pil = target_pil.resize((G.img_resolution, G.img_resolution), PIL.Image.LANCZOS)
    target_uint8 = np.array(target_pil, dtype=np.uint8)

    # Optimize projection.
    start_time = perf_counter()
    projected_w_steps = project_with_adversarial(
        G,
        target=torch.tensor(target_uint8.transpose([2, 0, 1]), device=device), # pylint: disable=not-callable
        num_steps=num_steps,
        device=device,
        verbose=True
    )
    print (f'Elapsed: {(perf_counter()-start_time):.1f} s')

    # Render debug output: optional video and projected image and W vector.
    # Render debug output: optional video and projected image and W vector.
    os.makedirs(outdir, exist_ok=True)
    if save_video:
        try:
            # OpenCV VideoWriter 설정
            frame_size = (target_uint8.shape[1] * 2, target_uint8.shape[0])  # 두 이미지를 나란히 표시
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # MP4 코덱
            video_path = f'{outdir}/proj.mp4'
            video = cv2.VideoWriter(video_path, fourcc, 30, frame_size)
            print(f'Saving optimization progress video "{video_path}"')

            # First frame
            first_synth = G.synthesis(projected_w_steps[0].unsqueeze(0), noise_mode='const')
            first_synth = (first_synth + 1) * (255/2)
            first_synth = first_synth.permute(0, 2, 3, 1).clamp(0, 255).to(torch.uint8)[0].cpu().numpy()
            
            # OpenCV는 BGR 형식을 사용하므로 RGB에서 BGR로 변환
            target_bgr = cv2.cvtColor(target_uint8, cv2.COLOR_RGB2BGR)
            first_synth_bgr = cv2.cvtColor(first_synth, cv2.COLOR_RGB2BGR)
            
            # 이미지 연결 및 저장
            combined_frame = np.concatenate([target_bgr, first_synth_bgr], axis=1)
            video.write(combined_frame)

            # Remaining frames
            for projected_w in projected_w_steps:
                synth_image = G.synthesis(projected_w.unsqueeze(0), noise_mode='const')
                synth_image = (synth_image + 1) * (255/2)
                synth_image = synth_image.permute(0, 2, 3, 1).clamp(0, 255).to(torch.uint8)[0].cpu().numpy()
                
                # RGB에서 BGR로 변환
                synth_image_bgr = cv2.cvtColor(synth_image, cv2.COLOR_RGB2BGR)
                
                # 이미지 연결 및 저장
                combined_frame = np.concatenate([target_bgr, synth_image_bgr], axis=1)
                video.write(combined_frame)

            # 비디오 파일 닫기
            video.release()
            
            # FFmpeg를 사용하여 비디오 재인코딩 (더 나은 호환성을 위해)
            try:
                import subprocess
                temp_path = f'{outdir}/proj_temp.mp4'
                os.rename(video_path, temp_path)
                subprocess.run([
                    'ffmpeg', '-i', temp_path, 
                    '-vcodec', 'libx264', 
                    '-acodec', 'aac', 
                    video_path
                ])
                os.remove(temp_path)
            except Exception as e:
                print(f"Warning: video re-encoding failed: {str(e)}")
                # 원본 파일을 유지
                os.rename(temp_path, video_path)
                
        except Exception as e:
            print(f'Warning: video save failed, skipping... Error: {str(e)}')


    # Save final projected frame and W vector.
    target_pil.save(f'{outdir}/target.png')
    projected_w = projected_w_steps[-1]
    synth_image = G.synthesis(projected_w.unsqueeze(0), noise_mode='const')
    synth_image = (synth_image + 1) * (255/2)
    synth_image = synth_image.permute(0, 2, 3, 1).clamp(0, 255).to(torch.uint8)[0].cpu().numpy()
    PIL.Image.fromarray(synth_image, 'RGB').save(f'{outdir}/proj.png')
    np.savez(f'{outdir}/projected_w.npz', w=projected_w.unsqueeze(0).cpu().numpy())

# ----------------------------------------------------------------------------

if __name__ == "__main__":
    run_projection() # pylint: disable=no-value-for-parameter

# ----------------------------------------------------------------------------
