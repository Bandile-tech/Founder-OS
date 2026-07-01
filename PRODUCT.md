# Product

## Register

product

## Users

Bandile Masocha — the sole user. A founder-athlete-student operating at high intensity across sprinting, academics, and business. Uses this daily as a command interface, not a passive dashboard. Context: alone, focused, often late at night or early morning. Needs a system that feels as serious as the work it tracks.

## Product Purpose

Founder OS is a personal operating system for disciplined execution — KPI tracking, habit logging, roadmap management, weekly targets, AI reflection, and vault. It is not a motivation app. It is a command interface. Success means the user can open it, know exactly where they stand across every domain, and act. The system scales psychologically from student → founder → group CEO without redesign.

## Brand Personality

Command-grade. Cinematic. Relentless.

The aesthetic reference is the Control AI Policy Platform (fuselab creative): deep space dark environment, glowing orbital node graphs, particle energy lines, cinematic zoom transitions, glassmorphism detail panels, animated stat counters. The feel is a high-stakes mission control room — not a productivity app, not a game, not a crypto dashboard. Serious, beautiful, alive.

## Anti-references

- **Notion / Linear**: too flat, too white, no energy — this system should feel like it has mass and motion
- **Crypto dashboards**: neon chaos, aggressive gradients, data overload — the motion here is precise, not frantic
- **Generic SaaS dark UI**: indigo accent, card grids, standard Tailwind admin — the default "dark mode" without soul

## Design Principles

1. **The system has mass** — every element should feel weighted and purposeful, not floaty or decorative
2. **Motion is information** — animations encode state changes (nodes expanding, counters climbing, lines activating); nothing animates for decoration alone
3. **Command, not consume** — layouts orient toward action and decision, not reading or browsing
4. **Cinematic restraint** — the aesthetic is cinematic but never loud; the video's particle effects serve the data, not the other way around
5. **Functional continuity** — all JS logic and data behaviour is untouched; only CSS, layout, and ambient visual layers change

## Accessibility & Inclusion

- Single-user tool; WCAG AA as a floor (readability matters for late-night use)
- All motion must respect `prefers-reduced-motion` — crossfade fallback for all particle/network animations
- Contrast: text remains legible against dark surfaces; glow accents supplement, never replace, text contrast
