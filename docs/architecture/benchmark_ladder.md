# MX Hard-Core Benchmark Ladder

Date: `2026-03-25`

This is a later track. It is not the next implementation frontier.

## Purpose

Create benchmark artifacts that are strong enough to convince skeptical systems engineers without turning MX into a benchmark-only project.

## Selection Rules

Every benchmark candidate must be:

- sliceable
- deterministic
- reproducibly testable
- attributable to MX
- bounded in blast radius
- mostly independent from external governance noise

## Ladder

### Tier 1: Bounded Systems Migrations

Examples:

- medium-size C/C++ to Rust slices
- strongly tested Python to Rust or Go slices
- protocol or CLI migration slices

Goal:

- prove MX can execute non-trivial systems migrations under strict verification

### Tier 2: Kernel-Adjacent Slices

Examples:

- driver-sized or subsystem-sized mirrored repos
- low-level runtime or tooling components with strict invariants

Goal:

- prove MX can handle hard constraints without upstream politics

### Tier 3: Rust-for-Linux Style Slice

Examples:

- sandboxed mirrored subsystem migration
- tightly bounded Rust-for-Linux-compatible slice

Goal:

- produce a serious systems benchmark artifact

### Tier 4: Broader Kernel Candidate

This is only eligible if prior tiers are repeatedly green and attributable.

## Required Benchmark Spec

Before any benchmark implementation begins, define:

- target repo or mirror
- exact slice boundary
- invariants
- verification commands
- allowed file scope
- acceptance rubric
- failure interpretation

## What Not To Do

Do not begin with:

- broad Linux kernel migration claims
- upstream-facing process work
- benchmarks whose success depends more on external governance than MX behavior

## Activation Gate

Track C becomes active only after:

- Track A phase 1 is complete
- Track B separation is sufficiently complete
- a benchmark spec is written and attributable
