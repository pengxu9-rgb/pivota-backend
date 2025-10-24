from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="pivota-agent",
    version="1.0.0",
    author="Pivota",
    author_email="support@pivota.com",
    description="Official Python SDK for Pivota Agent API",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/pivota/pivota-agent-sdk-python",
    project_urls={
        "Documentation": "https://docs.pivota.com/agent-sdk",
        "Source": "https://github.com/pivota/pivota-agent-sdk-python",
        "Tracker": "https://github.com/pivota/pivota-agent-sdk-python/issues",
    },
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.8",
    install_requires=[
        "requests>=2.25.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.18.0",
            "black>=22.0.0",
            "mypy>=0.950",
        ],
    },
    keywords="pivota agent ecommerce api sdk payments",
)




