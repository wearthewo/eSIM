"""Pure processing logic: parse, validate, QR, render, zip.

This package must not import FastAPI, Starlette, or anything from ``app.web``.
Activation codes and confirmation codes stay in memory only: never written to
disk, never logged, never persisted.
"""
