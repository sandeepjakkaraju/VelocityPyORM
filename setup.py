from setuptools import setup, find_packages

setup(
    name="velocity-py-orm",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "cryptography>=41.0.0",
    ],
    extras_require={
        "test": ["pytest>=7.0.0"],
    },
    author="sandeepkumarjakkaraju",
    description="A parallel Python ORM matching the architecture of Java VelocityORM",
)
