import torch.nn as nn
from torch.quantization import QuantStub, DeQuantStub


def _make_divisible(v, divisor, min_value=None):
    if min_value is None:
        min_value = divisor

    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)

    if new_v < 0.9 * v:
        new_v += divisor

    return new_v


class ConvBnReLU(nn.Sequential):
    def __init__(self, in_planes, out_planes, kernel_size=3, stride=1, groups=1):
        padding = (kernel_size - 1) // 2
        super(ConvBnReLU, self).__init__(
            nn.Conv2d(in_planes, 
                      out_planes, 
                      kernel_size, 
                      stride, 
                      padding, 
                      groups=groups, 
                      bias=False),
            nn.BatchNorm2d(out_planes, 
                           momentum=0.1),
            nn.ReLU(inplace=False)
        )


class InvertedResidual(nn.Module):
    """
    Origin   : channel 감소(1x1) => 계산(3x3) => channel 복구(1x1)
    Inverted : channel 증가(1x1) => 계산(3x3) => channel 감소(1x1)    
    """
    def __init__(self, in_planes, out_planes, stride, expand_ratio):
        super(InvertedResidual, self).__init__()
        assert stride in [1, 2]

        hidden_dim = int(round(in_planes * expand_ratio))
        self.use_res_connect = self.stride == 1 and in_planes == out_planes

        layers = []

        if expand_ratio != 1:
            # pw
            layers.append(ConvBnReLU(in_planes, 
                                     hidden_dim,
                                     kernel_size=1))

        layers.extend([
            # dw
            ConvBnReLU(hidden_dim, 
                       hidden_dim, 
                       stride=stride, 
                       groups=hidden_dim),
            
            # pw-linear
            nn.Conv2d(hidden_dim, 
                      out_planes, 
                      kernel_size=1, 
                      stride=1, 
                      padding=0, 
                      bias=False),

            nn.BatchNorm2d(out_planes, 
                           momentum=0.1),
        ])

        self.conv = nn.Sequential(*layers)
        self.skip_add = nn.quantized.FloatFunctional()

    def forward(self, x):
        if self.use_res_connect:
            return self.skip_add.add(x, self.conv(x))
        else:
            return self.conv(x)


class MobileNetV2(nn.Module):
    def __init__(self, 
                 num_classes=1000, 
                 width_mult=1.0, 
                 inverted_residual_setting=None,
                 round_nearest=8):
        super(MobileNetV2, self).__init__()

        block = InvertedResidual
        input_channel = 32
        last_channel = 1280

        if inverted_residual_setting is None:
            inverted_residual_setting = [
                ########################
                # t : Expansion ratio  #
                # c : channel          #
                # n : iteration        #
                # s : stride           #
                ########################
                [1, 16, 1, 1],
                [6, 24, 2, 2],
                [6, 32, 3, 2],
                [6, 64, 4, 2],
                [6, 96, 3, 1],
                [6, 160, 3, 2],
                [6, 320, 1, 1]
            ]
        
        if len(inverted_residual_setting) == 0 or len(inverted_residual_setting[0]) != 4:
            raise ValueError("inverted_residual_setting should be non-empty "
                             "or a 4-element list, got {}".format(inverted_residual_setting))

        # first layer
        input_channel = _make_divisible(input_channel * width_mult, round_nearest)
        self.last_channel = _make_divisible(last_channel * max(1.0, width_mult), round_nearest)

        features = [ConvBnReLU(3, input_channel, stride=2)]

        # inverted residual blocks
        for t, c, n, s in inverted_residual_setting:
            output_channel = _make_divisible(c * width_mult, round_nearest)
            for i in range(n):
                stride = s if i == 0 else 1
                features.append(block(input_channel, output_channel, stride, expand_ratio=t))
                input_channel = output_channel

        # last layer
        features.append(ConvBnReLU(input_channel, self.last_channel, kernel_size=1))

        self.features = nn.Sequential(*features)
        self.quant = QuantStub()
        self.dequant = DeQuantStub()

        # classifier
        self.classifier = nn.Sequential(nn.Dropout(0.2),
                                        nn.Linear(self.last_channel, num_classes))

        # weight init
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.zeros_(m.bias)

    def forward(self, x):
        x = self.quant(x)
        x = self.features(x)
        x = x.mean([2, 3])
        x = self.classifier(x)
        x = self.dequant(x)

        return x