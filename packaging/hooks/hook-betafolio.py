"""Collect only application assets and dynamically selected integrations."""
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = collect_data_files("betafolio", includes=["templates/**", "static/**"])
for package in ("tzdata", "exchange_calendars", "certifi", "curl_cffi", "ddgs", "tiktoken"):
    datas += collect_data_files(package)
for distribution in ("betafolio", "keyring", "yfinance", "ddgs", "langchain", "langchain-core",
                     "langchain-openai", "langchain-anthropic", "openai", "anthropic", "curl-cffi"):
    datas += copy_metadata(distribution, recursive=True)

hiddenimports = ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
                 "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on",
                 "langchain_openai", "langchain_anthropic", "yfinance", "openpyxl", "xlrd"]
for package in ("tiktoken_ext", "ddgs.engines", "exchange_calendars"):
    hiddenimports += collect_submodules(package)
hiddenimports += ["keyring.backends.Windows" if sys.platform == "win32" else "keyring.backends.macOS"]
