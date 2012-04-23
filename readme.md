binutils is a growing collection of helper scripts for making life easier.
Most of these are written in Python, with some written in bash. Pull requests 
are welcome. Be sure to include a description in the commands section below for 
new commands.

Target environment is OS X, but these scripts should work on any *NIX. Enjoy!

## Installation

Clone the repo and add the directory to your path. I use ~/.bash_profile for this.

## Commands

	decide [options]
		Choose an option from a whitespace separated list provided on the command line.
		Supports glob syntax.