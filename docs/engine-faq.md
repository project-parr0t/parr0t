<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

## Runtime font or layout evidence is required

- **Trigger:** Standalone Font replacement or conservative UI metric screening has passed, but runtime font ingestion, actual glyph rendering or a specified screen's layout remains unverified.
- **Automation boundary:** Serialized round trips and caller-supplied font metrics cannot establish Unity rendering, runtime font fallback, dynamic UI behavior or coverage at an actual display configuration.
- **Stop condition:** Do not claim runtime font or layout completion, change authored typography to hide a screening result, or install or launch a player automatically while the required observation is absent.
- **Required input or tool:** User-run observations for the explicitly requested screens and display configurations, with categorical font/glyph/layout results retained in the private workspace. Any agent-performed install or launch additionally needs its separate runtime approval.
- **Procedure:** Record the unverified screen/configuration scope privately, preserve the built artifact, and request that bounded user-run observation. Keep screenshots and title-specific findings private. If a defect is observed, bind the affected private inputs and apply only a confirmed correction before repeating the affected static check and observation.
- **Resume verification:** Verify the observation record refers to the current artifact and requested display configurations, and that each required font/glyph/layout result is categorical and successful. Revalidate the unchanged artifact binding and any corrected serialization or metric inputs. Completion covers only those observed screens and configurations.
- **Do not:** Infer all-screen or all-title coverage from a successful launch, substitute approximate metric results for actual rendering, bundle an unprovided font, or perform an unapproved runtime action.
