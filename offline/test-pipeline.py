#!/usr/bin/env python3
"""
End-to-end test of the offline solver pipeline using Ollama.

Skips the evens/server claim/submit cycle — just feeds problems directly
through the LLM + SymPy pipeline and prints the results.

Usage:
    python3 test-pipeline.py [--llama http://localhost:11434] [--model phi4-mini]
"""

import argparse
import sys
import time

sys.path.insert(0, ".")
import solver

PROBLEMS = [
    {
        "number": "1",
        "text": "Решите уравнение: x² - 5x + 6 = 0",
    },
    {
        "number": "2",
        "text": "Найдите производную функции f(x) = x³ + 2x² - x + 1",
    },
    {
        "number": "3",
        "text": "Вычислите определённый интеграл: ∫₀¹ (3x² + 2x) dx",
    },
    {
        "number": "4",
        "text": "Solve the system of equations: 2x + 3y = 12, x - y = 1",
    },
    {
        "number": "5",
        "text": "Simplify the expression: (x² - 4) / (x - 2)",
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--llama", default="http://localhost:11434")
    parser.add_argument("--model", default="phi4-mini")
    parser.add_argument("--problem", type=int, help="run only problem N (1-based)")
    args = parser.parse_args()

    solver.OLLAMA_MODEL = args.model

    problems = PROBLEMS
    if args.problem:
        problems = [PROBLEMS[args.problem - 1]]

    total_start = time.time()
    results = []

    for p in problems:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Problem {p['number']}: {p['text']}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        start = time.time()
        result = solver.solve_problem(args.llama, p, timeout=120)
        elapsed = time.time() - start

        results.append((p["number"], elapsed, result))
        print(f"\n--- Result ({elapsed:.1f}s) ---")
        print(result)

    total = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(results)} problems in {total:.1f}s")
    for num, elapsed, _ in results:
        print(f"  Problem {num}: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
