import copy
import os
from time import perf_counter

import click
import cv2
import numpy as np
import PIL.Image
import torch
import torch.nn.functional as F
import torchvision.models as models

import dnnlib
import legacy
from utils import lab_attack  # Lab Attack 함수 추가

def project(
    G,
    target: torch.Tensor,  # [C,H,W] and dynamic range [0,255], W & H must match G output resolution
    *,
    num_steps=1000,
    w_avg_samples=10000,
    attack_iters=100,  # Lab Attack 반복 횟수 추가
    initial_learning_rate=0.1,
    initial_noise_factor=0.05,
    lr_rampdown_length=0.25,
    lr_rampup_length=0.05,
    noise_ramp_length=0.75,
    regularize_noise_weight=1e5,
    verbose=False,
    device: torch.device
):
    assert target.shape == (G.img_channels, G.img_resolution, G.img_resolution)

    def logprint(*args):
        if verbose:
            print(*args)

    G = copy.deepcopy(G).eval().requires_grad_(False).to(device)  # type: ignore

    # Compute W stats.
    logprint(f'Computing W midpoint and stddev using {w_avg_samples} samples...')
    z_samples = np.random.RandomState(123).randn(w_avg_samples, G.z_dim)
    w_samples = G.mapping(torch.from_numpy(z_samples).to(device), None)  # [N, L, C]
    w_samples = w_samples[:, :1, :].cpu().numpy().astype(np.float32)  # [N, 1, C]
    w_avg = np.mean(w_samples, axis=0, keepdims=True)  # [1, 1, C]
    w_std = (np.sum((w_samples - w_avg) ** 2) / w_avg_samples) ** 0.5

    # Apply Lab Attack to target image
    target = target.unsqueeze(0).to(device).to(torch.float32)  # [1, C, H, W]
    target = (target / 255.0) * 2 - 1  # Normalize to [-1, 1] (GAN input format)

    # Generate adversarial example using Lab Attack
    target_adv, _ = lab_attack(target, torch.zeros(1, G.c_dim).to(device), G, iter=attack_iters)


    # Convert adversarial image back to [0, 255] range
    target_adv = ((target_adv + 1) / 2) * 255
    target_adv = target_adv.clamp(0, 255)

    # Ensure target_adv is in [1, C, H, W] shape
    if target_adv.ndim == 3:
        target_adv = target_adv.unsqueeze(0)  # Add batch dimension if missing

    assert target_adv.shape == (1, 3, G.img_resolution, G.img_resolution), \
        f"target_adv shape mismatch: {target_adv.shape}"

    # Setup noise inputs.
    noise_bufs = {name: buf for (name, buf) in G.synthesis.named_buffers() if 'noise_const' in name}

    def create_feature_extractor(device):
        resnet = models.resnet18(pretrained=True)
        feature_extractor = torch.nn.Sequential(
            *list(resnet.children())[:-1],
            torch.nn.Flatten()
        ).eval().to(device)

        for param in feature_extractor.parameters():
            param.requires_grad = False

        class FeatureExtractor(torch.nn.Module):
            def __init__(self, model):
                super().__init__()
                self.model = model
                self.register_buffer('mean', torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(device))
                self.register_buffer('std', torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(device))

            def forward(self, x):
                x = x / 255.0
                x = (x - self.mean) / self.std
                with torch.no_grad():
                    features = self.model(x).detach()
                return features.clone().requires_grad_(True)

        return FeatureExtractor(feature_extractor)

    feature_extractor = create_feature_extractor(device)

    # Compute features for adversarial target image
    target_features = feature_extractor(target_adv)

    w_opt = torch.tensor(w_avg, dtype=torch.float32, device=device, requires_grad=True)
    w_out = torch.zeros([num_steps] + list(w_opt.shape[1:]), dtype=torch.float32, device=device)
    optimizer = torch.optim.Adam([w_opt] + list(noise_bufs.values()), betas=(0.9, 0.999), lr=initial_learning_rate)

    # Init noise.
    for buf in noise_bufs.values():
        buf[:] = torch.randn_like(buf)
        buf.requires_grad = True

    for step in range(num_steps):
        t = step / num_steps
        w_noise_scale = w_std * initial_noise_factor * max(0.0, 1.0 - t / noise_ramp_length) ** 2
        lr_ramp = min(1.0, (1.0 - t) / lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        lr_ramp = lr_ramp * min(1.0, t / lr_rampup_length)
        lr = initial_learning_rate * lr_ramp
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        # Generate synthetic image
        w_noise = torch.randn_like(w_opt) * w_noise_scale
        ws = (w_opt + w_noise).repeat([1, G.mapping.num_ws, 1])
        synth_images = G.synthesis(ws, noise_mode='const')

        synth_images = (synth_images + 1) * (255 / 2)
        synth_features = feature_extractor(synth_images)

        dist = (target_features - synth_features).square().sum() * 0.01

        reg_loss = sum((v * torch.roll(v, shifts=1, dims=d)).mean() ** 2 for v in noise_bufs.values() for d in [2, 3])

        loss = dist + reg_loss * (regularize_noise_weight * 0.1)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()

        logprint(f'step {step+1:>4d}/{num_steps}: dist {dist:<4.2f} loss {float(loss):<5.2f}')
        w_out[step] = w_opt.detach()[0]

        with torch.no_grad():
            for buf in noise_bufs.values():
                buf -= buf.mean()
                buf *= buf.square().mean().rsqrt()

    return w_out.repeat([1, G.mapping.num_ws, 1])


#----------------------------------------------------------------------------

@click.command()
@click.option('--network', 'network_pkl', help='Network pickle filename', required=True)
@click.option('--target', 'target_fname', help='Target image file to project to', required=True, metavar='FILE')
@click.option('--num-steps',              help='Number of optimization steps', type=int, default=1000, show_default=True)
@click.option('--seed',                   help='Random seed', type=int, default=303, show_default=True)
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
    projected_w_steps = project(
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

    # video saving 부분을 다음과 같이 수정
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

#----------------------------------------------------------------------------

if __name__ == "__main__":
    run_projection() # pylint: disable=no-value-for-parameter

#----------------------------------------------------------------------------
