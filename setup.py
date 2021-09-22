from setuptools import setup, find_packages
import pathlib

here = pathlib.Path(__file__).parent.resolve()

# Get the long description from the README file
long_description = (here / 'README.md').read_text(encoding='utf-8')

setup(
    name='Webtoon_Downloader',
    version='0.2.1',
    description='Webtoons Scraper for downloading chapters of any series wanted.',
    author='Ali Taibi',
    author_email='zehinadev@gmail.com',
    url='https://github.com/Zehina/Webtoon-Downloader',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License: OSI Approved :: MIT License' 
    ],
    long_description=long_description,
    license='MIT',
    keyword='webtoon, downloader, scraper',
    packages=find_packages(where='src'), 
    package_dir={'': 'src'},
    python_requires='>=3.7, <4',
    install_requires=['requests>=2.26.0', 'beautifulsoup4>=4.10.0', 'rich>=10.10.0'],
    project_urls={
        'Bug Reports': 'https://github.com/Zehina/Webtoon-Downloader/issues',
        'Source': 'https://github.com/Zehina/Webtoon-Downloader/',
    },
)
