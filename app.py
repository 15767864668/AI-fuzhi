"""运行入口：python app.py。仅监听本机，不创建公网分享链接。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    project_root = Path(__file__).resolve().parent
    load_dotenv(project_root / ".env", override=False)
    # 相对路径始终基于程序目录，双击启动和命令行启动得到相同的数据目录。
    data_dir = Path(os.environ.get("HONGGUO_DATA_DIR", "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = project_root / data_dir
    from hongguo.canvas import start_canvas_server
    canvas_port = int(os.environ.get("HONGGUO_CANVAS_PORT", "8766"))
    canvas_server = start_canvas_server(data_dir, canvas_port)
    from hongguo.ui import build_ui

    app = build_ui(data_dir, canvas_port=canvas_port)
    app.launch(
        server_name="127.0.0.1",
        server_port=int(os.environ.get("HONGGUO_PORT", "7860")),
        share=False,
        inbrowser=os.environ.get("HONGGUO_OPEN_BROWSER", "1") == "1",
        allowed_paths=[str(data_dir.resolve())],
        show_error=True,
    )


if __name__ == "__main__":
    main()
