from setuptools import setup

setup(
    name = 'PyMetno',
    packages = ['metno'],
    install_requires=['xmltodict', 'aiohttp', 'async_timeout', 'pytz'],
    version = '0.8.2',
    description = 'A library to communicate with the met.no api',
    author='Daniel Hjelseth Høyer',
    url='https://github.com/Danielhiversen/pyMetno/',
    license='MIT',
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
