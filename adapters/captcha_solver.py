"""Zibll 滑块验证码处理逻辑。

将验证码相关算法与请求流程独立出来，
供 `ZibllSliderLoginAdapter` 调用。
"""

import json
import logging
import random
import string
from typing import List

import requests


class SliderCaptchaSolver:
    """处理 Zibll 滑块验证码：生成缺口、获取凭证、构造轨迹、生成提交字段。"""

    def __init__(self):
        self.logger = logging.getLogger("zibllcrawler.adapters.captcha_solver")

    @staticmethod
    def random_num(min_val: int, max_val: int) -> int:
        """JS 的 n(t,e) -> 返回 [min, max] 的随机整数（四舍五入）。"""
        return int(round(random.random() * (max_val - min_val) + min_val))

    @staticmethod
    def random_lowercase(length: int) -> str:
        """JS 的 a(t) -> 生成随机小写字母串。"""
        return ''.join(random.choices(string.ascii_lowercase, k=length))

    def js_i(self, t: int) -> str:
        """
        完全模拟 JS 的 i(t) 函数。
        t: 数值（缺口 x 坐标或滑动距离）
        返回：randstr / ticket 字符串
        """
        a = self.random_num(11, 40)
        b = self.random_num(11, 40)
        return f"{a}{self.random_lowercase(a)}{t}{self.random_lowercase(b)}{b}"

    def calc_slider_x(self, width: int = 280, slider_l: int = 42, slider_r: int = 9, offset: int = 3) -> int:
        """
        计算缺口水平位置 x（与 JS 一致）。
        范围：[sliderL+2*sliderR+offset+10, width-(sliderL+2*sliderR+offset+10)]
        """
        n = slider_l + 2 * slider_r + offset  # 63
        lo = n + 10
        hi = width - (n + 10)
        return self.random_num(lo, hi)

    def build_captcha_randstr(self, rand_str: str) -> str:
        """
        构建提交登录时的 captcha[randstr]。
        对应 JS 中：o + rand_str.substring(o, r) + r
        """
        a = self.random_num(1, 9)
        b = self.random_num(15, 25)
        if a >= b:  # 保底，确保有子串
            b = a + 10
        slice_ = rand_str[a:b]
        return f"{a}{slice_}{b}"

    def generate_trail(self, distance: int, length_range=(80, 130)) -> List[int]:
        """
        生成模拟垂直抖动轨迹（基于真实分布）。
        长度 80~130，数值集中在 0~2，偶尔波动。
        """
        length = random.randint(*length_range)
        trail = []
        for _ in range(length):
            val = random.choices(
                [0, 1, 2, -1, -2, 3, -3],
                weights=[0.4, 0.25, 0.1, 0.1, 0.05, 0.05, 0.05]
            )[0]
            trail.append(val)
        # 保证方差不为 0（避免机械直线）
        if all(v == 0 for v in trail):
            trail[random.randint(0, len(trail) - 1)] = random.choice([1, -1])
        return trail

    def solve(self, session: requests.Session, api_url: str) -> dict:
        """
        完成一次滑块验证码求解，返回登录所需字段。

        返回字段：
            - ticket
            - randstr
            - check
            - trail (JSON 字符串)
        """
        self.logger.info("开始滑块验证码求解")

        x = self.calc_slider_x(280)
        self.logger.debug(f"随机生成缺口位置 x = {x}")

        randstr = self.js_i(x)
        self.logger.debug(f"生成 randstr={randstr}，请求验证码凭证")

        captcha_url = f"{api_url}/wp-content/themes/zibll/action/captcha.php"
        params = {'type': 'slider', 'randstr': randstr}
        resp = session.get(captcha_url, params=params, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"获取验证码凭证失败，状态码: {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"验证码凭证响应非 JSON: {resp.text[:200]}") from exc

        token = data.get('token')
        server_rand_str = data.get('rand_str')
        check = data.get('check')
        if not all([token, server_rand_str, check]):
            raise RuntimeError(f"验证码凭证数据不完整: {data}")
        self.logger.debug(f"获取 token 成功，check={check}")

        # 模拟滑动距离（必须与 x 相差在 offset=8 以内）
        offset = 8
        distance = x + random.randint(-offset, offset)
        self.logger.debug(f"模拟滑动距离 distance = {distance}")

        trail = self.generate_trail(distance)
        self.logger.debug(f"生成轨迹，长度 = {len(trail)}")

        ticket = self.js_i(distance)
        captcha_randstr = self.build_captcha_randstr(server_rand_str)
        self.logger.debug(f"ticket={ticket[:10]}..., captcha_randstr={captcha_randstr}")

        return {
            'ticket': ticket,
            'randstr': captcha_randstr,
            'check': check,
            'trail': json.dumps(trail),
        }
