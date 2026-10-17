<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

## Runtime font or layout evidence is required

- **Trigger:** Static TMP/font/reference readback passes, but actual font ingestion, glyph rendering or a requested display configuration remains unobserved.
- **Automation boundary:** Serialized consistency and caller-supplied metrics cannot establish native font ingestion, shaping, fallback selection, dynamic UI layout or display-specific coverage.
- **Stop condition:** Keep runtime claims unverified for unobserved screens/configurations. Do not install or launch the player automatically, or change authored typography to hide an estimate.
- **Required input or tool:** Bounded user-run observations tied to the current artifact and specified screens/display configurations. Any agent-performed install or launch requires its own runtime authorization through the core guard.
- **Procedure:** Retain the artifact and its private binding. Record the unobserved scope privately, obtain the bounded observation, and retain categorical font/glyph/layout outcomes. If a defect is observed, make only a confirmed correction, revalidate the affected serialization and repeat the affected observation.
- **Resume verification:** Confirm that observation metadata binds the unchanged current artifact and every requested screen/configuration. Resume only when the required categorical outcomes succeed; the claim covers that observed scope only.
- **Do not:** Infer all-screen coverage from a successful launch, substitute static measurements for rendering, invent font licensing, inspect images for text without authorization, or bypass the core install/launch approval guard.
