"""站点适配性检测模块。

职责：
1. 检测单个目标网站是否启用 Zibll 滑块验证码。
2. 使用测试账号尝试登录接口，根据响应判断站点是否能被现有项目适配。
3. 支持批量检测（从文件读取 URL 列表）与单站检测（供导入模块调用）。
4. 支持自定义测试账号密码，以及别名可用性验证。

注意：本模块不依赖登录后的 session，也不属于某个账号的任务；
它回答的问题是"该站点能否被当前 Zibll 登录适配器复用"。
因此放在 `adapters/` 目录下，与登录适配器紧密相关。
"""

import json
import logging
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.exceptions import RequestException

from adapters.captcha_solver import SliderCaptchaSolver


logger = logging.getLogger("zibllcrawler.adapters.site_detector")

DEFAULT_TIMEOUT = 15


def _random_probe_username(length: int = 10) -> str:
    """生成随机的探测用用户名，避免在代码中硬编码任何账号。"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _random_probe_password(length: int = 16) -> str:
    """生成随机的探测用密码，避免在代码中硬编码任何密码。"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def normalize_url(url: str) -> str:
    """去掉首尾空白，确保不以内层路径结尾，保留 scheme://host。"""
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    netloc = parsed.netloc
    if not netloc:
        return url
    return f"{parsed.scheme}://{netloc}"


def _build_session(base_url: str, timeout: int = DEFAULT_TIMEOUT) -> requests.Session:
    """构造与登录适配器一致的 Session。"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Referer': base_url + '/',
    })
    return session


def _is_captcha_enabled(base_url: str, session: requests.Session, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """
    检测首页是否包含 Zibll 滑块验证码特征。

    检测特征：
    - machine-verification="slider"
    - slidercaptcha.min.js / captcha.min.js
    """
    try:
        with session.get(
            base_url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        ) as resp:
            chunk_size = 512 * 1024
            content = b''
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) >= chunk_size:
                    break

            try:
                text = content.decode(resp.encoding or 'utf-8', errors='ignore')
            except (UnicodeDecodeError, LookupError):
                text = content.decode('utf-8', errors='ignore')

            if 'machine-verification="slider"' in text:
                return True
            if 'slidercaptcha.min.js' in text or 'captcha.min.js' in text:
                return True
            return False
    except RequestException as exc:
        logger.debug(f"检测验证码时请求失败: {base_url}, {exc}")
        return False
    except Exception as exc:
        logger.debug(f"检测验证码时异常: {base_url}, {exc}")
        return False


def _prepare_login_payload(
    test_username: str,
    test_password: str,
    captcha_enabled: bool,
    captcha_fields: Optional[Dict] = None
) -> Dict[str, str]:
    """构造登录请求体。无验证码时只包含四个参数。"""
    payload = {
        'username': test_username,
        'password': test_password,
        'remember': 'forever',
        'action': 'user_signin',
    }
    if captcha_enabled and captcha_fields:
        payload.update({
            'captcha_mode': 'slider',
            'captcha[ticket]': captcha_fields['ticket'],
            'captcha[randstr]': captcha_fields['randstr'],
            'captcha[spliced]': 'true',
            'captcha[check]': captcha_fields['check'],
            'captcha[trail]': captcha_fields['trail'],
        })
    return payload


def _evaluate_login_response(resp: requests.Response) -> Dict:
    """解析登录响应并判断适配性。"""
    result = {
        'http_status': resp.status_code,
        'json_parsed': False,
        'error_code': None,
        'msg': '',
        'adaptable': False,
        'reason': '',
    }

    if resp.status_code != 200:
        result['reason'] = f"HTTP 状态码非 200: {resp.status_code}"
        return result

    try:
        data = resp.json()
        result['json_parsed'] = True
    except Exception as exc:
        result['reason'] = f"响应不是 JSON: {str(exc)[:80]}"
        return result

    result['error_code'] = data.get('error')
    result['msg'] = data.get('msg', '')
    msg = result['msg']

    # 登录成功，或已登录
    if result['error_code'] == 0 or '已经登录' in msg or '已登录' in msg:
        result['adaptable'] = True
        result['reason'] = '登录成功/已登录'
        return result

    # 接口正常返回业务错误，说明登录接口结构正确
    adaptable_errors = {
        '未找到此用户名',
        '用户名或密码错误',
        '用户不存在',
        '账号不存在',
        '密码错误',
        '账号密码错误',
    }
    if any(keyword in msg for keyword in adaptable_errors):
        result['adaptable'] = True
        result['reason'] = '账号不存在/密码错误，接口结构正确'
        return result

    result['reason'] = f"无法识别的响应: error={result['error_code']}, msg={msg[:80]}"
    return result


def _classify_failure(result: Dict) -> str:
    """根据检测结果判断失败是否属于通信层问题（如 DNS 失败、连接超时）。"""
    reason = result.get('reason', '')
    if not result.get('http_status'):
        return 'network'
    if '请求异常' in reason or '请求失败' in reason:
        return 'network'
    return 'logic'


class SiteDetector:
    """检测 Zibll 主题站点是否适配当前登录框架。"""

    def __init__(
        self,
        captcha_solver: SliderCaptchaSolver,
        timeout: int = DEFAULT_TIMEOUT,
        test_username: Optional[str] = None,
        test_password: Optional[str] = None,
    ):
        self.captcha_solver = captcha_solver
        self.timeout = timeout
        self.test_username = test_username or _random_probe_username()
        self.test_password = test_password or _random_probe_password()

    def detect(
        self,
        base_url: str,
        test_username: Optional[str] = None,
        test_password: Optional[str] = None,
    ) -> Dict:
        """
        检测单个站点是否适配。

        Args:
            base_url: 站点根 URL。
            test_username: 可选测试账号，未提供时使用构造时默认值。
            test_password: 可选测试密码。

        Returns:
            {
                'url': 规范化后的 URL,
                'captcha_enabled': bool,
                'adaptable': bool,
                'reason': str,
                'http_status': int | None,
                'error_code': any,
                'msg': str,
                'failure_type': 'network' | 'logic' | '',
            }
        """
        base_url = normalize_url(base_url)
        username = test_username or self.test_username
        password = test_password or self.test_password

        logger.info(f"开始检测: {base_url}")
        result = {
            'url': base_url,
            'captcha_enabled': False,
            'adaptable': False,
            'reason': '',
            'http_status': None,
            'error_code': None,
            'msg': '',
            'failure_type': '',
        }

        session = _build_session(base_url, self.timeout)
        try:
            warmup_resp = session.get(base_url, timeout=self.timeout, allow_redirects=True)
            if warmup_resp.status_code != 200:
                result['reason'] = f"首页访问失败: {warmup_resp.status_code}"
                result['failure_type'] = 'network' if warmup_resp.status_code >= 500 else 'logic'
                logger.info(f"检测结束: {base_url} -> 不适配, {result['reason']}")
                return result

            captcha_enabled = _is_captcha_enabled(base_url, session, self.timeout)
            result['captcha_enabled'] = captcha_enabled
            logger.info(f"验证码检测结果: {base_url}, enabled={captcha_enabled}")

            captcha_fields = None
            if captcha_enabled:
                try:
                    captcha_fields = self.captcha_solver.solve(session, base_url)
                except Exception as exc:
                    result['reason'] = f"验证码求解失败: {exc}"
                    result['failure_type'] = 'logic'
                    logger.info(f"检测结束: {base_url} -> 不适配, {result['reason']}")
                    return result

            login_url = f"{base_url}/wp-admin/admin-ajax.php"
            payload = _prepare_login_payload(username, password, captcha_enabled, captcha_fields)
            logger.info(f"提交测试登录: {base_url}")
            resp = session.post(login_url, data=payload, timeout=self.timeout)

            evaluation = _evaluate_login_response(resp)
            result.update(evaluation)
            result['failure_type'] = _classify_failure(result)

        except RequestException as exc:
            result['reason'] = f"请求异常: {exc}"
            result['failure_type'] = 'network'
        except Exception as exc:
            result['reason'] = f"检测异常: {exc}"
            result['failure_type'] = 'logic'

        logger.info(
            f"检测结束: {base_url} -> {'适配' if result['adaptable'] else '不适配'}, {result['reason']}"
        )
        return result

    def verify_alias(
        self,
        primary_url: str,
        alias_url: str,
        test_username: Optional[str] = None,
        test_password: Optional[str] = None,
    ) -> Dict:
        """
        验证别名 URL 是否可用。

        由于域名解析同站难以 100% 确认，这里以"可访问 + 被适配"作为可用标准：
        别名必须通过 detect() 检测且 adaptable=True。

        Returns:
            与 detect() 相同结构，额外包含 primary_url 字段。
        """
        result = self.detect(alias_url, test_username=test_username, test_password=test_password)
        result['primary_url'] = normalize_url(primary_url)
        return result

    def detect_from_file(
        self,
        input_path: str,
        output_path: str,
        max_workers: int = 4,
    ) -> List[Dict]:
        """
        从文件读取 URL 列表并批量检测，结果写入文件。

        Args:
            input_path: 每行一个 URL 的文本文件路径。
            output_path: 结果输出文件路径。
            max_workers: 并发检测线程数。

        Returns:
            所有检测结果列表。
        """
        input_path = Path(input_path)
        output_path = Path(output_path)

        if not input_path.exists():
            raise FileNotFoundError(f"输入文件不存在: {input_path}")

        raw_urls = [
            line.strip()
            for line in input_path.read_text(encoding='utf-8').splitlines()
            if line.strip()
        ]
        urls = []
        seen = set()
        for url in raw_urls:
            normalized = normalize_url(url)
            if normalized and normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)

        logger.info(f"读取到 {len(urls)} 个独立 URL，开始批量检测")
        results: List[Dict] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {
                executor.submit(self.detect, url): url for url in urls
            }
            for future in as_completed(future_to_url):
                try:
                    result = future.result()
                except Exception as exc:
                    url = future_to_url[future]
                    result = {
                        'url': url,
                        'captcha_enabled': False,
                        'adaptable': False,
                        'reason': f"并发执行异常: {exc}",
                        'http_status': None,
                        'error_code': None,
                        'msg': '',
                        'failure_type': 'logic',
                    }
                    logger.error(f"检测 {url} 时发生未捕获异常: {exc}")
                results.append(result)

        results.sort(key=lambda x: x['url'])
        self._write_results(results, output_path)
        return results

    @staticmethod
    def _write_results(results: List[Dict], output_path: Path) -> None:
        """将检测结果写入文本文件。"""
        compatible = [r for r in results if r['adaptable']]
        incompatible = [r for r in results if not r['adaptable']]

        lines = [
            "# zibllCrawler 站点适配性检测结果",
            "",
            f"总计: {len(results)} 个站点",
            f"适配: {len(compatible)} 个",
            f"不适配: {len(incompatible)} 个",
            "",
            "# 适配的站点列表",
        ]
        for r in compatible:
            lines.append(f"[OK] {r['url']} | 验证码: {'启用' if r['captcha_enabled'] else '未启用'} | {r['reason']}")

        lines.extend(["", "# 不适配的站点列表"])
        for r in incompatible:
            lines.append(f"[FAIL] {r['url']} | 验证码: {'启用' if r['captcha_enabled'] else '未知/未启用'} | {r['reason']}")

        lines.append("")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding='utf-8')
        logger.info(f"检测结果已写入: {output_path}")


def detect_sites(input_path: str, output_path: str, max_workers: int = 4) -> List[Dict]:
    """便捷入口：创建默认 SiteDetector 并批量检测。"""
    solver = SliderCaptchaSolver()
    detector = SiteDetector(captcha_solver=solver)
    return detector.detect_from_file(input_path, output_path, max_workers=max_workers)
