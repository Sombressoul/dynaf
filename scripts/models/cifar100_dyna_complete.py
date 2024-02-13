import torch
import torch.nn as nn

from dyna import ThetaInput, ThetaLinear, ModulatedActivation


class CIFAR100DyNAComplete(nn.Module):
    def __init__(
        self,
    ):
        super(CIFAR100DyNAComplete, self).__init__()

        count_modes = 7
        expected_range = [-5.0, +5.0]

        self.a_conv_pre = nn.Conv2d(3, 32, 3, 1, 1)
        self.a_activation_pre = ModulatedActivation(
            passive=True,
            count_modes=count_modes,
            features=32,
            expected_input_min=expected_range[0],
            expected_input_max=expected_range[1],
        )
        self.a_conv_post = nn.Conv2d(32, 32, 3, 2, 1)
        self.a_activation_post = ModulatedActivation(
            passive=True,
            count_modes=count_modes,
            features=32,
            expected_input_min=expected_range[0],
            expected_input_max=expected_range[1],
        )
        self.a_layer_norm = nn.LayerNorm([16, 16])

        self.b_conv_pre = nn.Conv2d(32, 32, 3, 1, 1)
        self.b_activation_pre = ModulatedActivation(
            passive=True,
            count_modes=count_modes,
            features=32,
            expected_input_min=expected_range[0],
            expected_input_max=expected_range[1],
        )
        self.b_conv_post = nn.Conv2d(32, 32, 3, 2, 1)
        self.b_activation_post = ModulatedActivation(
            passive=True,
            count_modes=count_modes,
            features=32,
            expected_input_min=expected_range[0],
            expected_input_max=expected_range[1],
        )
        self.b_layer_norm = nn.LayerNorm([8, 8])

        self.c_conv_pre = nn.Conv2d(32, 32, 3, 1, 1)
        self.c_activation_pre = ModulatedActivation(
            passive=True,
            count_modes=count_modes,
            features=32,
            expected_input_min=expected_range[0],
            expected_input_max=expected_range[1],
        )
        self.c_conv_post = nn.Conv2d(32, 32, 3, 2, 1)

        self.d_input = ThetaInput(
            in_features=512,
            out_features=96,
            theta_modes=count_modes,
            theta_expected_input_min=expected_range[0],
            theta_expected_input_max=expected_range[1],
        )
        self.d_linear = ThetaLinear(
            in_features=96,
            out_features=96,
            theta_components_in=count_modes,
            theta_modes_out=count_modes,
            theta_full_features=True,
        )
        self.d_activation = ModulatedActivation(
            passive=False,
        )
        self.d_batch_norm = nn.BatchNorm1d(96)

        self.e_linear = ThetaLinear(
            in_features=96,
            out_features=100,
            theta_components_in=count_modes,
            theta_modes_out=count_modes,
            theta_full_features=True,
        )
        self.e_activation = ModulatedActivation(
            passive=False,
        )
        self.e_batch_norm = nn.BatchNorm1d(100)

        self.output_linear = nn.Linear(100, 100)

        self.dropout = nn.Dropout(p=0.25)
        self.log_softmax = nn.LogSoftmax(dim=1)

        pass

    def forward(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:
        x = x.contiguous()

        x = self.a_conv_pre(x)
        x = torch.permute(x, [0, 2, 3, 1])
        x = self.a_activation_pre(x).x
        x = torch.permute(x, [0, 3, 1, 2])
        x = self.a_conv_post(x)
        x = torch.permute(x, [0, 2, 3, 1])
        x = self.a_activation_post(x).x
        x = torch.permute(x, [0, 3, 1, 2])
        x = self.a_layer_norm(x)

        x = self.b_conv_pre(x)
        x = torch.permute(x, [0, 2, 3, 1])
        x = self.b_activation_pre(x).x
        x = torch.permute(x, [0, 3, 1, 2])
        x = self.b_conv_post(x)
        x = torch.permute(x, [0, 2, 3, 1])
        x = self.b_activation_post(x).x
        x = torch.permute(x, [0, 3, 1, 2])
        x = self.b_layer_norm(x)

        x = self.c_conv_pre(x)
        x = torch.permute(x, [0, 2, 3, 1])
        x = self.c_activation_pre(x).x
        x = torch.permute(x, [0, 3, 1, 2])
        x = self.c_conv_post(x)

        x = x.flatten(1)
        x = self.dropout(x)

        signal = self.d_input(x)
        signal = self.d_linear(signal)
        signal = self.d_activation(signal)

        signal = self.e_linear(signal)
        signal = self.e_activation(signal)
        x = signal.x

        x = self.output_linear(x)
        x = self.log_softmax(x)

        return x
