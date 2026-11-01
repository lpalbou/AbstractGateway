# Console app illustrations

Card illustrations for the console's app list, one per app id (`<app_id>.webp`).

## Format

- 768 x 432 px (16:9), RGB WebP, quality 80, no alpha, each under 150 KB.
- Generated at 1024 x 576 px, then resized with Pillow (Lanczos) and encoded as WebP (`method=6`).
- No text, letters, logos or UI in any image.

## Generation

- Generator: [MLX-Gen](https://github.com/lpalbou/mlx-gen) (`mlxgen`, mlx-gen 0.37.0), text-to-image on Apple Silicon.
- Model: `AbstractFramework/flux.2-klein-9b-8bit` (distilled FLUX.2 Klein 9B, 8-bit).
- Steps 4, guidance 1.0, 1024 x 576. The distilled model does not accept a negative prompt; exclusions are stated in the shared style suffix instead.
- Every prompt is the app subject followed by the shared style suffix below, so the set stays one visual family.

Shared style suffix:

```text
Abstract minimalist 3D render on a deep midnight navy to near-black background, soft volumetric glow, glass and light filaments, subtle film grain, calm premium editorial illustration, centered subject with generous dark negative space, cinematic wide composition. Purely visual: no text, no letters, no numbers, no symbols, no logos, no user interface.
```

## Per-app prompt and seed

| App id | Seed | Subject (prepended to the style suffix) |
|---|---|---|
| `observer` | 202 | A luminous glass lens like a calm watchful eye observing a horizontal timeline of glowing beads of light flowing past it, amber and gold accent light. |
| `continuum` | 505 | A gently winding river of light flowing in a graceful S-curve, made of several parallel glowing lanes carrying small rounded glass tiles, merging and continuing endlessly into the distance, teal and cyan accent light. |
| `code` | 505 | Floating stacked translucent glass slabs engraved with indented rows of short glowing bars like abstract lines of code, floating calmly in dark space, the top slab softly glowing as if safely preserved, emerald green accent light. |
| `entity` | 202 | A serene luminous humanoid silhouette made of soft light, surrounded by a constellation of glowing nodes connected by fine threads forming a memory graph, violet and magenta accent light. |
| `flow` | 202 | A diagram made of light: rounded glowing rectangular nodes connected by smooth curved light beams with tiny particles travelling along them, electric blue accent light. |
| `note` | 101 | Small floating translucent paper cards drifting and linking together with fine glowing threads into a growing knowledge web, warm coral and peach accent light. |
| `assistant` | 202 | A friendly softly glowing orb hovering in space, gentle concentric ripples of light radiating from it like a calm voice in conversation, sky blue and white accent light. |

## Regenerate

Download the model once (`mlxgen download --model AbstractFramework/flux.2-klein-9b-8bit`), then for one app, with `PROMPT` set to its subject followed by the style suffix and `SEED` from the table:

```sh
mlxgen generate --model AbstractFramework/flux.2-klein-9b-8bit --prompt "$PROMPT" --width 1024 --height 576 --steps 4 --guidance 1.0 --seed $SEED --output app.png && python3 -c "from PIL import Image; Image.open('app.png').convert('RGB').resize((768, 432), Image.LANCZOS).save('<app_id>.webp', 'WEBP', quality=80, method=6)"
```

Only one image-generation process should run at a time on a machine that also serves models.
