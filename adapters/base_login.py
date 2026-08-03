"""登录适配器。

提供通用基类 `BaseLoginAdapter` 与通用的 Zibll 滑块验证码登录实现
`ZibllSliderLoginAdapter`。通用版本假设 Cookie 预热与登录 API
均使用同一域名，适用于大多数 Zibll 主题站点。
"""

import logging
from typing import Optional

import requests

from adapters.captcha_solver import SliderCaptchaSolver
from core.db_pool import SQLiteConnectionPool


class BaseLoginAdapter:
    """所有登录适配器的基类。"""

    def __init__(self, db_pool: Optional[SQLiteConnectionPool] = None):
        self.logger = logging.getLogger("zibllcrawler.adapters.base_login")
        self.db_pool = db_pool

    def login(self, site_account_id: int, base_url: str, username: str, password: str,
              cookie: Optional[str] = None) -> Optional[requests.Session]:
        """执行登录。子类必须实现。"""
        raise NotImplementedError

    def execution_url(self, base_url: str) -> str:
        """
        返回业务执行时应使用的站点 URL。
        大多数适配器直接使用 base_url；子类可覆盖以处理跨域等特例。
        """
        return base_url


class ZibllSliderLoginAdapter(BaseLoginAdapter):
    """通用的 Zibll 主题站点滑块验证码登录适配器（Cookie 与 API 同域）。"""

    def __init__(self, captcha_solver: SliderCaptchaSolver,
                 db_pool: Optional[SQLiteConnectionPool] = None):
        super().__init__(db_pool)
        self.captcha_solver = captcha_solver

    def _build_session(self, base_url: str) -> requests.Session:
        """创建并配置 requests.Session。"""
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

    def _load_cookies(self, session: requests.Session, cookie: str) -> None:
        """从数据库加载 Cookie 到 Session。"""
        for part in cookie.split(';'):
            if '=' not in part:
                continue
            k, v = part.strip().split('=', 1)
            session.cookies.set(k, v)
        self.logger.debug("已从数据库加载 Cookie")

    def _is_session_valid(self, session: requests.Session, base_url: str, username: str) -> bool:
        """
        检查当前 Session 是否已登录有效，并确认登录用户与预期账号一致。

        Zibll 主题提供 `action=get_current_user` 接口，登录后返回：
        - is_logged_in 为 true
        - id > 0
        - user_data.user_login 与传入 username 一致
        """
        check_url = f"{base_url}/wp-admin/admin-ajax.php"
        payload = {"action": "get_current_user"}
        try:
            resp = session.post(check_url, data=payload, timeout=10)
            if resp.status_code != 200:
                self.logger.debug(f"Session 探测接口返回非 200: {resp.status_code}")
                return False
            data = resp.json()
            # 兼容不同版本：优先使用 is_logged_in，不存在则回退到 id + user_data 判断
            if data.get("is_logged_in") is False:
                self.logger.debug(f"Session 明确未登录: {data}")
                return False
            if data.get("id", 0) <= 0 or not data.get("user_data"):
                self.logger.debug(f"Session 未处于登录状态: {data}")
                return False
            user_login = (data.get("user_data") or {}).get("user_login")
            if user_login != username:
                self.logger.warning(
                    f"Cookie 登录用户不匹配: cookie 对应 '{user_login}'，期望 '{username}'"
                )
                return False
            self.logger.info(f"Session 有效: 用户 {user_login}")
            return True
        except Exception as exc:
            self.logger.debug(f"Session 有效性检查失败: {exc}")
            return False

    def _warmup_cookies(self, session: requests.Session, base_url: str) -> None:
        """访问主页获取 Cookie。"""
        self.logger.info(f"访问 {base_url} 获取 Cookie")
        resp = session.get(base_url, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"主页访问失败，状态码: {resp.status_code}")
        cookies = session.cookies.get_dict()
        self.logger.debug(f"获取到 Cookie: {cookies}")

    def _update_last_used(self, site_account_id: int) -> None:
        """仅更新最后使用时间，不修改 Cookie。"""
        if self.db_pool is None:
            return
        conn = self.db_pool.get_connection()
        conn.execute(
            "UPDATE site_accounts SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
            (site_account_id,)
        )
        conn.commit()
        self.logger.debug(f"已更新 site_account_id={site_account_id} 最后使用时间")

    def _update_cookie_in_db(self, site_account_id: int, session: requests.Session) -> None:
        """将登录后的 Cookie 写回数据库。"""
        if self.db_pool is None:
            return
        cookie_str = '; '.join(f"{k}={v}" for k, v in session.cookies.get_dict().items())
        conn = self.db_pool.get_connection()
        conn.execute(
            "UPDATE site_accounts SET cookie = ?, last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
            (cookie_str, site_account_id)
        )
        conn.commit()
        self.logger.debug(f"已更新 site_account_id={site_account_id} 的 Cookie")

    def login(self, site_account_id: int, base_url: str, username: str, password: str,
              cookie: Optional[str] = None) -> Optional[requests.Session]:
        """
        获取有效的已登录 Session。

        流程：
        1. 若数据库中存在 Cookie，先探测其有效性；有效则直接复用。
        2. Cookie 失效时，若存在密码则执行完整登录流程。
        3. Cookie 失效且无密码时，直接返回 None，由调用方决定是否停用账号。
        """
        self.logger.info(
            f"开始登录 site_account_id={site_account_id}, url={base_url}, user={username}"
        )

        # 1. 若数据库有 Cookie，先探测是否仍有效
        if cookie:
            session = self._build_session(base_url)
            self._load_cookies(session, cookie)
            if self._is_session_valid(session, base_url, username):
                self.logger.info("数据库 Cookie 仍有效，跳过登录")
                self._update_last_used(site_account_id)
                return session
            self.logger.info("数据库 Cookie 已失效，执行重新登录")

        # 没有密码且 Cookie 失效，无法继续登录
        if not password:
            self.logger.warning(
                f"site_account_id={site_account_id} 未设置密码且 Cookie 失效，无法登录"
            )
            return None

        # 2. 执行完整登录流程
        session = self._build_session(base_url)
        self._warmup_cookies(session, base_url)
        captcha_fields = self.captcha_solver.solve(session, base_url)

        login_url = f"{base_url}/wp-admin/admin-ajax.php"
        payload = {
            'username': username,
            'password': password,
            'captcha_mode': 'slider',
            'remember': 'forever',
            'action': 'user_signin',
            'captcha[ticket]': captcha_fields['ticket'],
            'captcha[randstr]': captcha_fields['randstr'],
            'captcha[spliced]': 'true',
            'captcha[check]': captcha_fields['check'],
            'captcha[trail]': captcha_fields['trail'],
        }
        self.logger.info("提交登录请求")
        resp = session.post(login_url, data=payload, timeout=15)
        try:
            result = resp.json()
        except Exception as exc:
            raise RuntimeError(f"登录响应非 JSON: {resp.text[:500]}") from exc

        if result.get('error') == 0:
            self.logger.info("登录成功")
            self._update_cookie_in_db(site_account_id, session)
            return session

        # 已持有有效 Cookie 时，站点可能直接返回“已登录”
        msg = result.get('msg', '')
        if '已经登录' in msg or '已登录' in msg:
            self.logger.info(f"检测到已登录状态: {msg}")
            self._update_cookie_in_db(site_account_id, session)
            return session

        self.logger.error(f"登录失败: {msg or '未知错误'}")
        return None
