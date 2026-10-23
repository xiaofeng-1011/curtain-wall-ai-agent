"""幕墙方案助手 —— 聊天式铝型材比选（v0.3）。

运行：python -m streamlit run app.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402



def load_secrets() -> None:
    """从 .streamlit/secrets.toml 读取密钥等配置；环境变量已设置时以环境变量为准。"""
    try:
        items = dict(st.secrets)
    except Exception:  # noqa: BLE001 - 没有 secrets.toml 时正常继续
        return
    for k in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "MW_PROVIDER", "MW_MODEL", "MW_DEEPSEEK_THINKING"):
        if items.get(k) and not os.environ.get(k):
            os.environ[k] = str(items[k]).strip()


load_secrets()

from ui.chat import main  # noqa: E402

# 读入用户保存的模型配置（%LOCALAPPDATA%\MqHelper\config.toml），环境变量优先
try:
    from agent import extraction  # noqa: E402
    extraction.load_model_config()
except Exception:  # noqa: BLE001
    pass

main()
