"""创建或更新演示账号，密码只从终端读取。"""

import argparse
import getpass

from app.auth import AuthStore
from app.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="创建或更新演示账号")
    parser.add_argument("username")
    parser.add_argument("--role", required=True, choices=("teacher", "student"))
    parser.add_argument("--student-id")
    args = parser.parse_args()
    password = getpass.getpass("密码: ")
    if password != getpass.getpass("再次输入密码: "):
        parser.error("两次密码不一致")
    AuthStore(get_settings().auth_db_path).put_account(
        args.username, password, args.role, args.student_id
    )
    print("账号已更新，旧会话已失效")


if __name__ == "__main__":
    main()
