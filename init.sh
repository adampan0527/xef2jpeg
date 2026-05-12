#!/bin/bash

# init.sh - Development environment startup and testing script for XEF2JPEG
# This script sets up the development environment and runs basic tests

set -e  # Exit on error

echo "=========================================="
echo "XEF2JPEG Development Environment"
echo "=========================================="

# Navigate to the script's directory
cd "$(dirname "$0")"

# Check Python installation
echo "Checking Python installation..."
if ! command -v python &> /dev/null; then
    echo "ERROR: Python is not installed or not in PATH"
    echo "Please install Python 3.8+ from https://www.python.org/"
    exit 1
fi

PYTHON_VERSION=$(python --version 2>&1)
echo "Found: $PYTHON_VERSION"

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
if [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "ERROR: Could not find virtual environment activation script"
    exit 1
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "=========================================="
echo "Development Environment Ready"
echo "=========================================="
echo ""
echo "To run the application:"
echo "  python xef2jpeg.py"
echo ""
echo "To run with a specific XEF file:"
echo "  python xef2jpeg.py path/to/file.xef"
echo ""

# Basic end-to-end test
echo "=========================================="
echo "Running Basic Validation Tests"
echo "=========================================="

# Test 1: Check if main module can be imported
echo "1. Testing module imports..."
python -c "import tkinter; print('   ✓ tkinter available')" || {
    echo "   ✗ tkinter not available"
    echo "   NOTE: tkinter is required but may not be installed"
    echo "   On Ubuntu/Debian: sudo apt-get install python3-tk"
    echo "   On Windows: Reinstall Python with tkinter option checked"
}

# Test 2: Check if Pillow is installed
echo "2. Testing Pillow installation..."
python -c "from PIL import Image; print('   ✓ Pillow available')" || {
    echo "   ✗ Pillow not available"
    echo "   Run: pip install Pillow"
}

# Test 3: Check if xef2jpeg.py has valid syntax
echo "3. Testing xef2jpeg.py syntax..."
python -m py_compile xef2jpeg.py && echo "   ✓ Syntax OK" || echo "   ✗ Syntax error"

# Test 4: Check if feature_list.json is valid JSON
echo "4. Testing feature_list.json format..."
python -c "import json; json.load(open('feature_list.json')); print('   ✓ Valid JSON')" || echo "   ✗ Invalid JSON"

# Test 5: Verify input and output directories exist or can be created
echo "5. Checking input/output directories..."
if [ -d "XEF2JPEG_Input" ]; then
    XEF_COUNT=$(find XEF2JPEG_Input -name "*.xef" 2>/dev/null | wc -l)
    echo "   ✓ XEF2JPEG_Input exists ($XEF_COUNT .xef files found)"
else
    echo "   ✗ XEF2JPEG_Input directory missing"
    mkdir -p XEF2JPEG_Input
    echo "   ✓ Created XEF2JPEG_Input"
fi

if [ -d "XEF2JPEG_Output" ]; then
    echo "   ✓ XEF2JPEG_Output exists"
else
    echo "   ✗ XEF2JPEG_Output directory missing"
    mkdir -p XEF2JPEG_Output
    echo "   ✓ Created XEF2JPEG_Output"
fi

# Test 6: Count features in feature_list.json
echo "6. Checking feature list..."
FEATURE_COUNT=$(python -c "import json; data=json.load(open('feature_list.json')); print(len(data['features']))")
echo "   ✓ Total features: $FEATURE_COUNT"

echo ""
echo "=========================================="
echo "Basic Tests Complete"
echo "=========================================="
echo ""
echo "To start the GUI application, run:"
echo "  python xef2jpeg.py"
echo ""
echo "Note: This is a Windows desktop application."
echo "On Linux/macOS, the GUI may have limited functionality."
echo ""
