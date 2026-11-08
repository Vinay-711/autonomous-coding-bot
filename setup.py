from setuptools import setup

setup(
    name="autocoder-ai",
    version="1.0.0",
    py_modules=["bot"],
    install_requires=[
        "google-genai>=2.34.0",
        "playwright>=1.40.0",
        "pydantic>=2.0.0",
        "pyperclip>=1.8.0",
    ],
    entry_points={
        "console_scripts": [
            "autocoder=bot:cli",
        ],
    },
)

