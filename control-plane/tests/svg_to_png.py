#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SVG to PNG Converter for MCP Server Icons
Supports multiple sizes and optimization
"""

import base64
import cairosvg
from pathlib import Path
from PIL import Image
from io import BytesIO
from typing import Optional, Tuple


def svg_to_png_file(
        svg_path: str,
        output_path: str,
        size: int = 512,
        background: Optional[str] = None
) -> Path:
    """
    Convert SVG to PNG file

    Args:
        svg_path: Path to input SVG file
        output_path: Path for output PNG file
        size: Output size in pixels (square)
        background: Optional background color (hex, e.g., '#FFFFFF')

    Returns:
        Path to created PNG file
    """
    # Read SVG content
    with open(svg_path, 'r') as f:
        svg_content = f.read()

    # Add background if specified
    if background:
        # Inject background rectangle
        svg_content = svg_content.replace(
            '<svg',
            f'<svg><rect width="100%" height="100%" fill="{background}"/>'
        )

    # Convert to PNG
    cairosvg.svg2png(
        bytestring=svg_content.encode('utf-8'),
        write_to=output_path,
        output_width=size,
        output_height=size
    )

    print(f"✓ Created: {output_path} ({size}x{size}px)")
    return Path(output_path)


def svg_to_png_bytes(
        svg_path: str,
        size: int = 512
) -> bytes:
    """
    Convert SVG to PNG bytes (for in-memory operations)

    Args:
        svg_path: Path to input SVG file
        size: Output size in pixels

    Returns:
        PNG data as bytes
    """
    png_bytes = cairosvg.svg2png(
        url=svg_path,
        output_width=size,
        output_height=size
    )
    return png_bytes


def svg_to_base64(
        svg_path: str,
        size: int = 512
) -> str:
    """
    Convert SVG to base64-encoded PNG for API upload

    Args:
        svg_path: Path to input SVG file
        size: Output size in pixels

    Returns:
        Base64 string with data URI prefix
    """
    png_bytes = svg_to_png_bytes(svg_path, size)
    base64_str = base64.b64encode(png_bytes).decode('utf-8')
    return f"data:image/png;base64,{base64_str}"


def create_multiple_sizes(
        svg_path: str,
        output_dir: str = ".",
        sizes: list = None
) -> dict:
    """
    Create multiple PNG sizes from one SVG

    Args:
        svg_path: Path to input SVG file
        output_dir: Directory for output files
        sizes: List of sizes to generate (default: [128, 256, 512, 1024])

    Returns:
        Dictionary mapping size to file path
    """
    if sizes is None:
        sizes = [128, 256, 512, 1024]

    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    svg_name = Path(svg_path).stem
    results = {}

    for size in sizes:
        output_path = output_dir / f"{svg_name}_{size}.png"
        svg_to_png_file(svg_path, str(output_path), size)
        results[size] = output_path

    return results


def optimize_png(
        png_path: str,
        quality: int = 85
) -> None:
    """
    Optimize PNG file size using Pillow

    Args:
        png_path: Path to PNG file
        quality: Quality level (1-95)
    """
    img = Image.open(png_path)

    # Convert RGBA to RGB if no transparency
    if img.mode == 'RGBA':
        # Check if image has transparency
        extrema = img.getextrema()
        if len(extrema) == 4 and extrema[3] == (255, 255):
            # No transparency, convert to RGB
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[3])
            img = rgb_img

    # Save optimized
    img.save(png_path, 'PNG', optimize=True, quality=quality)
    print(f"✓ Optimized: {png_path}")


# =============================================================================
# Main execution
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python svg_to_png.py <svg_file> [output_file] [size]")
        print("\nExamples:")
        print("  python svg_to_png.py icon.svg")
        print("  python svg_to_png.py icon.svg output.png")
        print("  python svg_to_png.py icon.svg output.png 1024")
        sys.exit(1)

    svg_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else svg_file.replace('.svg', '.png')
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 512

    # Convert
    print(f"Converting {svg_file}...")
    svg_to_png_file(svg_file, output_file, size)

    # Optimize
    optimize_png(output_file)

    print(f"\n✅ Done! Created {output_file}")