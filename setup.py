from setuptools import setup

setup(
    name = 'PyMetno',
    packages = ['metno'],
    install_requires=['xmltodict', 'aiohttp', 'async_timeout', 'pytz'],
    version = '0.7.0',
    description = 'A library to communicate with the met.no api',
    author='Daniel Hoyer Iversen',
    url='https://github.com/Danielhiversen/pyMetno/',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Other Environment',
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Home Automation',
        'Topic :: Software Development :: Libraries :: Python Modules'
        ]
)
