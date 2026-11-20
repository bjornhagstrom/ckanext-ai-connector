import re
from pathlib import Path

from setuptools import find_namespace_packages, setup


HERE = Path(__file__).parent
REPOSITORY = "https://github.com/bjornhagstrom/ckanext-ai-connector/blob/main/"


def long_description():
    """The README for PyPI, with links that work outside the repository."""
    text = (HERE / "README.md").read_text(encoding="utf-8")
    return re.sub(r"\]\((?!https?://|#|mailto:)([^)]+)\)", lambda m: "](" + REPOSITORY + m.group(1) + ")", text)


setup(
    name="ckanext-ai-connector",
    version="0.1.0",
    description="One AI connection (model, key, time limits) that CKAN extensions share, set up by a sysadmin",
    long_description=long_description(),
    long_description_content_type="text/markdown",
    author="Bjorn Hagstrom",
    author_email="bjorn@hagstrom.nu",
    url="https://github.com/bjornhagstrom/ckanext-ai-connector",
    project_urls={
        "Source": "https://github.com/bjornhagstrom/ckanext-ai-connector",
        "Changelog": "https://github.com/bjornhagstrom/ckanext-ai-connector/blob/main/CHANGELOG.md",
        "Issues": "https://github.com/bjornhagstrom/ckanext-ai-connector/issues",
    },
    packages=find_namespace_packages(include=["ckanext", "ckanext.*"]),
    # The legacy declaration of the ckanext namespace, as CKAN and most extensions have it: the wheel then installs a
    # -nspkg.pth file that adds this package's directory to ckanext's path. Without it, a CKAN whose ckanext/__init__.py uses
    # pkg_resources.declare_namespace (setuptools before 81) does not see the package unless another extension declares it.
    namespace_packages=["ckanext"],
    include_package_data=True,
    # litellm talks to the AI services; 1.102.1 is the first release tested here that installs on CKAN 2.12's Python 3.14
    # (releases 1.83.8 - 1.93.0.dev3 declare Requires-Python <3.14). cryptography encrypts keys entered on the settings page.
    # jsonschema checks a structured answer against the requested shape.
    install_requires=["litellm>=1.102.1,<2", "cryptography>=42,<51", "jsonschema>=4,<5"],
    # Kept so that `pip install ckanext-ai-connector[ai]` (older instructions) still works; it adds nothing.
    extras_require={"ai": []},
    license="MIT",
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
    ],
    entry_points={
        "ckan.plugins": [
            "ai_connector=ckanext.ai_connector.plugin:AIConnectorPlugin",
        ],
    },
)
