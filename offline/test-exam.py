#!/usr/bin/env python3
"""
Test the offline solver pipeline with the 2024 entrance exam.
"""

import sys
import time

sys.path.insert(0, ".")
import solver

EXAM_MARKDOWN = r"""# Вступительное испытание по высшей математике 2024 год

## 1
Пусть f(x) — решение дифференциального уравнения f''(x) = 0 с начальными значениями f(0) = 2, f'(0) = 1. Найти f(5).

## 2
Вычислить значение производной f'(z) в точке z = i для функции комплексной переменной f(z) = (1+z)^9.

## 3
Найти площадь, ограниченную прямыми y = 1 и x = 8 и кривой y = x^(1/3) (кубический корень из x).

## 4
Найти минимальное и максимальное значение на интервале (0, 2) функции
f(x) = arccos(1 - x^2/2) + 2*arccot(x / sqrt(4 - x^2)).

## 5
Проводятся две независимые серии испытаний Бернулли. В первой из них n1 = 900 испытаний, а вероятность успеха в каждом p1 = 0.1. Во второй n2 = 300 испытаний, а вероятность успеха в каждом p2 = 0.3. С помощью теоремы Муавра — Лапласа оценить вероятность того, что во второй серии будет не менее, чем на пять успехов больше, чем в первой. Ответ выразить через Phi(x) — функцию распределения стандартной нормальной случайной величины.

## 6
Определитель матрицы 6x6 раскладывается в сумму 6! произведений по 6 в каждом, с учётом знака перестановки. Какое максимальное количество из этих произведений отлично от нуля, если все элементы главной диагонали матрицы равны нулю? Какое максимальное количество из этих произведений положительно?
"""


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--llama", default="http://localhost:11434")
    parser.add_argument("--model", default="phi4-mini")
    parser.add_argument("--problem", type=int, help="run only problem N (1-based)")
    args = parser.parse_args()

    solver.OLLAMA_MODEL = args.model

    problems = solver.parse_problems(EXAM_MARKDOWN)
    print(f"Parsed {len(problems)} problems from exam", file=sys.stderr)

    if args.problem:
        problems = [p for p in problems if p["number"] == str(args.problem)]

    total_start = time.time()
    results = []

    for p in problems:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Problem {p['number']}: {p['text'][:80]}...", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        start = time.time()
        result = solver.solve_problem(args.llama, p, timeout=180)
        elapsed = time.time() - start

        results.append((p["number"], elapsed, result))
        print(f"\n--- Problem {p['number']} ({elapsed:.1f}s) ---")
        print(result)

    total = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"EXAM SUMMARY: {len(results)} problems in {total:.0f}s")
    for num, elapsed, _ in results:
        print(f"  Problem {num}: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
