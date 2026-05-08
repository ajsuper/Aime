#!/bin/bash

# Run this script whenever you make changes to source.

CONFIG_DIR = $HOME/.config/aime-assistant/

mkdir $CONFIG_DIR
mkdir ./build/

# Copies all default configuration files to the specified config dir.
cp ./resources/default-config/* $CONFIG_DIR/

# Compile the c++ binary
g++ ./src/serve.cpp -lsqlite3 -o ./build/serve.o







