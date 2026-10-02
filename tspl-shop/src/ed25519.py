"""Ed25519 signatures (RFC 8032) in pure Python — no dependencies.

The same code as Licensing/keygen.py in the app project, so keys issued by the
shop and by hand are identical in format and both verify in the app.
"""

import hashlib

_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _sha512(m):
    return hashlib.sha512(m).digest()


def _xrecover(y):
    xx = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = 4 * pow(5, _p - 2, _p) % _p
_Bx = _xrecover(_By)
_B = (_Bx, _By, 1, _Bx * _By % _p)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _p
    b = (y1 + x1) * (y2 + x2) % _p
    c = t1 * 2 * _d * t2 % _p
    dd = z1 * 2 * z2 % _p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _p, g * h % _p, f * g % _p, e * h % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _encode_point(P):
    x, y, z, _ = P
    zi = pow(z, _p - 2, _p)
    x, y = x * zi % _p, y * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, 'little')


def _expand(seed):
    h = _sha512(seed)
    a = int.from_bytes(h[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_from_seed(seed):
    a, _ = _expand(seed)
    return _encode_point(_mul(a, _B))


def sign(seed, msg):
    """Deterministic signature: the same seed and message always give the same bytes."""
    a, prefix = _expand(seed)
    A = _encode_point(_mul(a, _B))
    r = int.from_bytes(_sha512(prefix + msg), 'little') % _L
    R = _encode_point(_mul(r, _B))
    k = int.from_bytes(_sha512(R + A + msg), 'little') % _L
    S = (r + k * a) % _L
    return R + int.to_bytes(S, 32, 'little')
