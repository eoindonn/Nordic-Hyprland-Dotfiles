"""CSS-style cubic-bezier easing for hand-driven animations."""


def cubic_bezier(x1: float, y1: float, x2: float, y2: float):
    """Return f(t) -> eased progress, like CSS `cubic-bezier(x1, y1, x2, y2)`."""

    def sample(a1: float, a2: float, t: float) -> float:
        return 3 * a1 * (1 - t) ** 2 * t + 3 * a2 * (1 - t) * t**2 + t**3

    def slope(a1: float, a2: float, t: float) -> float:
        return 3 * a1 * (1 - t) ** 2 + 6 * (a2 - a1) * (1 - t) * t + 3 * (1 - a2) * t**2

    def ease(x: float) -> float:
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        # solve sample_x(t) = x with Newton's method, then bisection fallback
        t = x
        for _ in range(8):
            error = sample(x1, x2, t) - x
            d = slope(x1, x2, t)
            if abs(error) < 1e-5 or abs(d) < 1e-6:
                break
            t -= error / d
        if not 0 <= t <= 1 or abs(sample(x1, x2, t) - x) > 1e-4:
            lo, hi = 0.0, 1.0
            for _ in range(30):
                t = (lo + hi) / 2
                if sample(x1, x2, t) < x:
                    lo = t
                else:
                    hi = t
        return sample(y1, y2, t)

    return ease


# Material 3 motion: things entering decelerate in, things leaving accelerate out
EMPHASIZED_DECELERATE = cubic_bezier(0.05, 0.7, 0.1, 1.0)
EMPHASIZED_ACCELERATE = cubic_bezier(0.3, 0.0, 0.8, 0.15)
STANDARD = cubic_bezier(0.2, 0.0, 0.0, 1.0)
