#!/usr/bin/env python3
"""Verify Scout Agent tool registration and core modules."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.tools.registry import ToolRegistry

# Discover all tools
ToolRegistry.discover()

# Get all registered tools
tools = ToolRegistry.all_tools()

print("✅ Scout Agent Tool Registry Verification")
print("=" * 60)
print(f"Total Registered Tools: {len(tools)}")
print("=" * 60)

for name, tool in sorted(tools.items()):
    print(f"  • {name}: {tool.description[:50]}...")

print("=" * 60)
print("Verification Complete!")
