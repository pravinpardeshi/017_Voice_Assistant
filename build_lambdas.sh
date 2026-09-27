#!/bin/bash
# Build deployment zips for both Lambda functions.
# Run from project root: bash build_lambdas.sh
set -e

PROJECT_ROOT="$(pwd)"
DEPLOY_DIR="/tmp/lambda-deploy"
rm -rf "$DEPLOY_DIR"
rm -f "$PROJECT_ROOT/lex-hook.zip" "$PROJECT_ROOT/s3-saver.zip"

echo "Building lex-hook.zip..."
mkdir -p "$DEPLOY_DIR"
cp "$PROJECT_ROOT/config.py" "$DEPLOY_DIR/config.py"
cp "$PROJECT_ROOT/lambda/lex_hook.py" "$DEPLOY_DIR/lambda_function.py"
cd "$DEPLOY_DIR" && zip -r "$PROJECT_ROOT/lex-hook.zip" .
rm -rf "$DEPLOY_DIR"/*

echo "Building s3-saver.zip..."
mkdir -p "$DEPLOY_DIR"
cp "$PROJECT_ROOT/config.py" "$DEPLOY_DIR/config.py"
cp "$PROJECT_ROOT/lambda/s3_saver.py" "$DEPLOY_DIR/lambda_function.py"
cd "$DEPLOY_DIR" && zip -r "$PROJECT_ROOT/s3-saver.zip" .
rm -rf "$DEPLOY_DIR"

echo "Done."
ls -lh "$PROJECT_ROOT/lex-hook.zip" "$PROJECT_ROOT/s3-saver.zip"
