"""Generate visual similarity report for embedding quality evaluation.

Outputs to data/outputs/:
  1. similarity_heatmap.png — NxN cosine similarity matrix of all input images
  2. search_results.png    — Top matches for each request image with scores
  3. report.txt            — Text summary of all scores

Usage:
  source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11
  python scripts/generate_similarity_report.py
"""

import asyncio
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from PIL import Image

# Setup path
sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT_DIR = Path(__file__).parent.parent
INPUT_DIR = PROJECT_DIR / "data" / "inputs"
REQUEST_DIR = PROJECT_DIR / "data" / "request"
OUTPUT_DIR = PROJECT_DIR / "data" / "outputs"


def get_images(directory: Path):
    """Get sorted image paths from directory."""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    return sorted([
        p for p in directory.iterdir()
        if p.suffix.lower() in exts
    ])


def short_name(path: Path, max_len: int = 18) -> str:
    name = path.stem
    return name[:max_len] if len(name) > max_len else name


async def main():
    from src.infrastructure.config import get_settings
    from src.infrastructure.embedding.clip_embedder import CLIPEmbedder

    settings = get_settings()
    embedder = CLIPEmbedder(settings)
    await embedder.initialize()

    input_images = get_images(INPUT_DIR)
    request_images = get_images(REQUEST_DIR)

    if not input_images:
        print(f"No images found in {INPUT_DIR}")
        return

    print(f"Embedding {len(input_images)} input images...")

    # ── Embed all input images ──
    input_vectors = []
    input_names = []
    for img_path in input_images:
        vec = await embedder.generate_embedding(f"file://{img_path}")
        if vec is not None:
            input_vectors.append(vec)
            input_names.append(short_name(img_path))
        else:
            print(f"  SKIP: {img_path.name} (embedding failed)")

    if not input_vectors:
        print("No valid embeddings generated.")
        return

    input_matrix = np.array(input_vectors)
    print(f"  {len(input_vectors)} embeddings generated ({input_matrix.shape[1]}-dim)")

    # ── 1. Similarity Heatmap (input × input) ──
    print("\nGenerating similarity heatmap...")
    sim_matrix = np.dot(input_matrix, input_matrix.T)

    fig, ax = plt.subplots(figsize=(max(10, len(input_names) * 0.7), max(8, len(input_names) * 0.6)))
    im = ax.imshow(sim_matrix, cmap="RdYlGn", vmin=0.0, vmax=1.0, aspect="auto")

    ax.set_xticks(range(len(input_names)))
    ax.set_yticks(range(len(input_names)))
    ax.set_xticklabels(input_names, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(input_names, fontsize=8)

    # Add score text in cells
    for i in range(len(input_names)):
        for j in range(len(input_names)):
            score = sim_matrix[i, j]
            color = "white" if score > 0.7 or score < 0.3 else "black"
            ax.text(j, i, f"{score:.2f}", ha="center", va="center",
                    fontsize=6, color=color)

    plt.colorbar(im, ax=ax, label="Cosine Similarity")
    ax.set_title("CLIP Embedding Similarity Matrix (Input Images)", fontsize=12, pad=15)
    plt.tight_layout()

    heatmap_path = OUTPUT_DIR / "similarity_heatmap.png"
    plt.savefig(heatmap_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {heatmap_path}")

    # ── 2. Search Results Visualization ──
    if request_images:
        print(f"\nGenerating search results for {len(request_images)} request images...")

        for req_path in request_images:
            req_vec = await embedder.generate_embedding(f"file://{req_path}")
            if req_vec is None:
                print(f"  SKIP: {req_path.name}")
                continue

            # Compute similarities to all inputs
            scores = np.dot(input_matrix, req_vec)
            ranked_indices = np.argsort(scores)[::-1]

            # Top 8 matches
            top_k = min(8, len(ranked_indices))
            top_indices = ranked_indices[:top_k]

            fig = plt.figure(figsize=(20, 5))
            gs = gridspec.GridSpec(1, top_k + 1, width_ratios=[1.5] + [1] * top_k)

            # Query image
            ax_query = fig.add_subplot(gs[0])
            query_img = Image.open(req_path)
            ax_query.imshow(query_img)
            ax_query.set_title(f"QUERY\n{short_name(req_path)}", fontsize=9, fontweight="bold")
            ax_query.axis("off")

            # Top matches
            for k, idx in enumerate(top_indices):
                ax = fig.add_subplot(gs[k + 1])
                match_path = input_images[idx]
                match_img = Image.open(match_path)
                ax.imshow(match_img)

                score = scores[idx]
                color = "green" if score >= 0.8 else ("orange" if score >= 0.5 else "red")
                ax.set_title(f"#{k+1} ({score:.3f})\n{short_name(match_path)}", fontsize=8, color=color)
                ax.axis("off")

            plt.suptitle(f"Search Results: {req_path.name}", fontsize=12, y=1.02)
            plt.tight_layout()

            result_path = OUTPUT_DIR / f"search_results_{req_path.stem}.png"
            plt.savefig(result_path, dpi=150, bbox_inches="tight")
            plt.close()
            print(f"  Saved: {result_path}")

    # ── 3. Text Report ──
    print("\nGenerating text report...")
    report_path = OUTPUT_DIR / "report.txt"
    with open(report_path, "w") as f:
        f.write("CLIP Embedding Quality Report\n")
        f.write(f"Model: {settings.clip_model_name}\n")
        f.write(f"Vector dimension: {input_matrix.shape[1]}\n")
        f.write(f"Input images: {len(input_vectors)}\n")
        f.write(f"Request images: {len(request_images)}\n")
        f.write("=" * 60 + "\n\n")

        # Input similarity stats
        upper = sim_matrix[np.triu_indices_from(sim_matrix, k=1)]
        f.write("INPUT SIMILARITY STATS (upper triangle, excl. diagonal)\n")
        f.write(f"  Mean:   {upper.mean():.4f}\n")
        f.write(f"  Std:    {upper.std():.4f}\n")
        f.write(f"  Min:    {upper.min():.4f}\n")
        f.write(f"  Max:    {upper.max():.4f}\n")
        f.write(f"  Median: {np.median(upper):.4f}\n\n")

        # High similarity pairs (potential duplicates)
        f.write("HIGH SIMILARITY PAIRS (> 0.85, potential duplicates)\n")
        pairs = []
        for i in range(len(input_names)):
            for j in range(i + 1, len(input_names)):
                if sim_matrix[i, j] > 0.85:
                    pairs.append((input_names[i], input_names[j], sim_matrix[i, j]))
        if pairs:
            for a, b, s in sorted(pairs, key=lambda x: -x[2]):
                f.write(f"  {a} <-> {b}: {s:.4f}\n")
        else:
            f.write("  None found\n")
        f.write("\n")

        # Search results
        if request_images:
            f.write("SEARCH RESULTS\n")
            for req_path in request_images:
                req_vec = await embedder.generate_embedding(f"file://{req_path}")
                if req_vec is None:
                    continue
                scores = np.dot(input_matrix, req_vec)
                ranked = np.argsort(scores)[::-1]

                f.write(f"\n  Query: {req_path.name}\n")
                for k, idx in enumerate(ranked[:10]):
                    f.write(f"    #{k+1:2d} {input_names[idx]:20s}  {scores[idx]:.4f}\n")

    print(f"  Saved: {report_path}")

    await embedder.cleanup()
    print(f"\nDone! All outputs in {OUTPUT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
