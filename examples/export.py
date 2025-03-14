# Copyright (c) Chris Choy (chrischoy@ai.stanford.edu).
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Please cite "4D Spatio-Temporal ConvNets: Minkowski Convolutional Neural
# Networks", CVPR'19 (https://arxiv.org/abs/1904.08755) if you use any part
# of the code.
import os
import argparse
import math
import numpy as np
from urllib.request import urlretrieve

try:
    import open3d as o3d
except ImportError:
    raise ImportError('Please install open3d with `pip install open3d`.')

import torch
import MinkowskiEngine as ME
from examples.minkunet import MinkUNet34C

# Check if the weights and file exist and download
if not os.path.isfile('weights.pth'):
    print('Downloading weights...')
    urlretrieve("https://bit.ly/2O4dZrz", "weights.pth")
if not os.path.isfile("1.ply"):
    print('Downloading an example pointcloud...')
    urlretrieve("https://bit.ly/3c2iLhg", "1.ply")

parser = argparse.ArgumentParser()
parser.add_argument('--file_name', type=str, default='1.ply')
parser.add_argument('--weights', type=str, default='weights.pth')
parser.add_argument('--use_cpu', action='store_true')

CLASS_LABELS = ('wall', 'floor', 'cabinet', 'bed', 'chair', 'sofa', 'table',
                'door', 'window', 'bookshelf', 'picture', 'counter', 'desk',
                'curtain', 'refrigerator', 'shower curtain', 'toilet', 'sink',
                'bathtub', 'otherfurniture')

VALID_CLASS_IDS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 24, 28, 33, 34, 36, 39
]

SCANNET_COLOR_MAP = {
    0: (0., 0., 0.),
    1: (174., 199., 232.),
    2: (152., 223., 138.),
    3: (31., 119., 180.),
    4: (255., 187., 120.),
    5: (188., 189., 34.),
    6: (140., 86., 75.),
    7: (255., 152., 150.),
    8: (214., 39., 40.),
    9: (197., 176., 213.),
    10: (148., 103., 189.),
    11: (196., 156., 148.),
    12: (23., 190., 207.),
    14: (247., 182., 210.),
    15: (66., 188., 102.),
    16: (219., 219., 141.),
    17: (140., 57., 197.),
    18: (202., 185., 52.),
    19: (51., 176., 203.),
    20: (200., 54., 131.),
    21: (92., 193., 61.),
    22: (78., 71., 183.),
    23: (172., 114., 82.),
    24: (255., 127., 14.),
    25: (91., 163., 138.),
    26: (153., 98., 156.),
    27: (140., 153., 101.),
    28: (158., 218., 229.),
    29: (100., 125., 154.),
    30: (178., 127., 135.),
    32: (146., 111., 194.),
    33: (44., 160., 44.),
    34: (112., 128., 144.),
    35: (96., 207., 209.),
    36: (227., 119., 194.),
    37: (213., 92., 176.),
    38: (94., 106., 211.),
    39: (82., 84., 163.),
    40: (100., 85., 144.),
}


def load_file(file_name):
    pcd = o3d.io.read_point_cloud(file_name)
    coords = np.array(pcd.points)
    colors = np.array(pcd.colors)
    return coords, colors, pcd


def normalize_color(color: torch.Tensor, is_color_in_range_0_255: bool = False) -> torch.Tensor:
    r"""
    Convert color in range [0, 1] to [-0.5, 0.5]. If the color is in range [0,
    255], use the argument `is_color_in_range_0_255=True`.

    `color` (torch.Tensor): Nx3 color feature matrix
    `is_color_in_range_0_255` (bool): If the color is in range [0, 255] not [0, 1], normalize the color to [0, 1].
    """
    if is_color_in_range_0_255:
        color /= 255
    color -= 0.5
    return color.float()

def EnsureDirExists(dir):
    if not os.path.exists(dir):
        print("Creating %s" % dir)
        os.makedirs(dir)

# assuming uniform kernel size, stride, padding
# assuming input_data is 5D tensor, in sparse tensor format
def im2col_3d(input_sparse, kernel_size, stride=1, padding=0):
    assert stride == 1, "stride must be 1"
    # convert to dense tensor
    input_data, min_coordinate, tensor_stride = input_sparse.dense(min_coordinate=torch.IntTensor([0, 0, 0]))

    if padding > 0:
        input_data = torch.nn.functional.pad(input_data, (padding, padding, padding, padding, padding, padding))

    # Get input dimensions
    batch_size, channels, depth, height, width = input_data.size()

    # Calculate the dimensions of the output matrix
    out_depth = math.ceil((depth - kernel_size + 1) / stride)
    out_height = math.ceil((height - kernel_size + 1) / stride)
    out_width = math.ceil((width - kernel_size + 1) / stride)

    # Create the output matrix
    # not enforcing output sparsity pattern
    col_unconstrained = torch.zeros(
        (batch_size, channels, kernel_size, kernel_size, kernel_size, out_depth, out_height, out_width),
        device=input_data.device
    )
    # enforced output sparsity pattern
    col = torch.zeros(
        (batch_size, channels, kernel_size, kernel_size, kernel_size, out_depth, out_height, out_width),
        device=input_data.device
    )

    # Fill the output matrix with patches from the input data
    for z in range(0, kernel_size):
        z_max = z + stride * out_depth
        for y in range(0, kernel_size):
            y_max = y + stride * out_height
            for x in range(0, kernel_size):
                x_max = x + stride * out_width
                col_unconstrained[:, :, z, y, x, :, :, :] = input_data[:, :, z:z_max:stride, y:y_max:stride, x:x_max:stride]

    # enforce output sparsity pattern
    # only non zero output when input at the same coordinate is non-zero
    for i in range(input_sparse.coordinates.shape[0]):
        crds = input_sparse.coordinates[i].tolist()
        batch = crds[0]
        # sparse_crd = min_crd + tensor_stride * dense_crd
        z = (crds[1] - min_coordinate[0, 0]) // tensor_stride[0]
        y = (crds[2] - min_coordinate[0, 1]) // tensor_stride[1]
        x = (crds[3] - min_coordinate[0, 2]) // tensor_stride[2]
        col[batch, :, :, :, :, z, y, x] = col_unconstrained[batch, :, :, :, :, z, y, x]

    col = col.view(batch_size, -1, out_depth*out_height*out_width).transpose(1, 2).contiguous()
    col = col.view(batch_size*out_depth*out_height*out_width, -1)

    return col

def get_activation(name, mode, dir, in_activation, out_activation, kernel_size, stride, unsqueeze=False):
    def in_hook(model, input, output):
        for i in range(len(input)):
            in_activation[name+"."+str(i)] = input[i].detach()
            input_dense = input[i].dense(min_coordinate=torch.IntTensor([0, 0, 0]))[0]
            print(f"[in] {name=} in{i} {input[i].shape=} {input_dense.shape=} density={torch.count_nonzero(input[i].features)/input[i].features.numel()}")

            # Get input dimensions
            batch_size, channels, depth, height, width = input_dense.size()
            if stride == 1: # can only do stride 1
                col = im2col_3d(input[i].detach(), kernel_size, stride, kernel_size//2)
                print(f"[im2col] {name=} {kernel_size=} {stride=} {col.shape=} {torch.count_nonzero(col)=} density={torch.count_nonzero(col)/col.numel()}")
                np.save(f"{tensor_dir}/in/{name}.{i}.npy", col.cpu().numpy())

    def out_hook(model, input, output):
        out_activation[name] = output.detach()
        print(f"[out] {name=} {output.shape=} {output.dense(min_coordinate=torch.IntTensor([0, 0, 0]))[0].shape=}")
        # TODO: save the output
        #saveTensor(args, name, mode, output.detach(), unsqueeze)
    if mode == 'in':
        return in_hook
    elif mode == 'out':
        return out_hook
    else:
        assert False

if __name__ == '__main__':
    config = parser.parse_args()
    device = torch.device('cuda' if (
        torch.cuda.is_available() and not config.use_cpu) else 'cpu')
    print(f"Using {device}")
    # Define a model and load the weights
    model = MinkUNet34C(3, 20).to(device)
    model_dict = torch.load(config.weights, map_location=torch.device('cpu'))
    model.load_state_dict(model_dict)
    model.eval()

    tensor_dir = f"/scratch/yifany/spmspm/inputs/MinkUNet34C"
    EnsureDirExists(os.path.join(tensor_dir, 'weight'))
    EnsureDirExists(os.path.join(tensor_dir, 'in'))
    EnsureDirExists(os.path.join(tensor_dir, 'out'))

    in_activation = {}
    out_activation = {}
    hooks = []
    for n, m in model.named_modules():
        # export conv
        if isinstance(m, ME.MinkowskiConvolution):
            print("[weight]", n, m, m.kernel.shape)
            #saveTensor(args, n, 'weight', m.weight) # alexnet have bias, ignore it for now
            weight = m.kernel.detach().transpose(0, 1).contiguous()
            K = weight.shape[-1]
            weight = weight.view(-1, K).cpu().numpy()
            print("[weight reshaped]", n, m, weight.shape)
            np.save(f"{tensor_dir}/weight/{n}.npy", weight)

            handle1 = m.register_forward_hook(get_activation(n, 'in', tensor_dir, in_activation, out_activation, m.kernel_generator.kernel_size[0], m.kernel_generator.kernel_stride[0]))
            handle2 = m.register_forward_hook(get_activation(n, 'out', tensor_dir, in_activation, out_activation, m.kernel_generator.kernel_size[0], m.kernel_generator.kernel_stride[0]))
            hooks.append(handle1)
            hooks.append(handle2)

    coords, colors, pcd = load_file(config.file_name)
    # Measure time
    with torch.no_grad():
        voxel_size = 0.02
        # Feed-forward pass and get the prediction
        in_field = ME.TensorField(
            features=normalize_color(torch.from_numpy(colors)),
            coordinates=ME.utils.batched_coordinates([coords / voxel_size], dtype=torch.float32),
            quantization_mode=ME.SparseTensorQuantizationMode.UNWEIGHTED_AVERAGE,
            minkowski_algorithm=ME.MinkowskiAlgorithm.SPEED_OPTIMIZED,
            device=device,
        )
        # Convert to a sparse tensor
        sinput = in_field.sparse()
        # Output sparse tensor
        soutput = model(sinput)
        print(sinput.shape)
        print(soutput.shape)
        # get the prediction on the input tensor field
        out_field = soutput.slice(in_field)
        logits = out_field.F

    _, pred = logits.max(1)
    pred = pred.cpu().numpy()

    # Create a point cloud file
    pred_pcd = o3d.geometry.PointCloud()
    # Map color
    colors = np.array([SCANNET_COLOR_MAP[VALID_CLASS_IDS[l]] for l in pred])
    pred_pcd.points = o3d.utility.Vector3dVector(coords)
    pred_pcd.colors = o3d.utility.Vector3dVector(colors / 255)
    pred_pcd.estimate_normals()

    # Move the original point cloud
    pcd.points = o3d.utility.Vector3dVector(
        np.array(pcd.points) + np.array([0, 5, 0]))

