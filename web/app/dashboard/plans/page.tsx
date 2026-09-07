import Link from "next/link";

export const dynamic = "force-dynamic";

/**
 * Credit plans.
 *
 * Presentation only. There is no billing provider wired up behind this yet,
 * so nothing here takes a payment or changes an account — and the page says
 * so plainly rather than showing a Buy button that would do nothing. When
 * billing is connected, the CTA on each card is the only thing that changes.
 */

const PLANS = [
  {
    name: "Starter",
    price: "$19",
    cadence: "per month",
    credits: "40 videos a month",
    blurb: "For testing the format and posting a few times a week.",
    features: [
      "40 generation credits",
      "Both channels",
      "Arabic and English narration",
      "1080×1920 downloads",
      "Email support",
    ],
    featured: false,
  },
  {
    name: "Creator",
    price: "$49",
    cadence: "per month",
    credits: "150 videos a month",
    blurb: "For posting daily across more than one account.",
    features: [
      "150 generation credits",
      "Everything in Starter",
      "Re-Create from a video",
      "Scheduled publishing",
      "Priority rendering queue",
    ],
    featured: true,
  },
  {
    name: "Studio",
    price: "$149",
    cadence: "per month",
    credits: "600 videos a month",
    blurb: "For running several channels as a pipeline.",
    features: [
      "600 generation credits",
      "Everything in Creator",
      "Unlimited connected accounts",
      "Analytics history",
      "Priority support",
    ],
    featured: false,
  },
] as const;

export default function PlansPage() {
  return (
    <>
      <div
        className="page-head"
        data-art="create"
        style={{ ["--head-art" as string]: "url('/channels/create.jpg')" }}
      >
        <h1>
          Buy <span className="hl">credits</span>
        </h1>
        <p>One credit is one finished video. Credits do not expire.</p>
      </div>

      <div className="notice" style={{ marginBottom: 22 }}>
        Billing is not connected yet, so these plans cannot be purchased. Your
        account is on unlimited preview access and every generation is free.
      </div>

      <div className="plan-grid">
        {PLANS.map((plan) => (
          <div
            key={plan.name}
            className={`plan${plan.featured ? " is-featured" : ""}`}
          >
            {plan.featured && <span className="plan-flag">Most popular</span>}
            <h3>{plan.name}</h3>
            <p className="plan-blurb">{plan.blurb}</p>

            <p className="plan-price">
              {plan.price}
              <span>/{plan.cadence.replace("per ", "")}</span>
            </p>
            <p className="plan-credits">{plan.credits}</p>

            <ul className="plan-features">
              {plan.features.map((f) => (
                <li key={f}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
                    <path
                      d="m5 12 5 5L19 7"
                      stroke="currentColor"
                      strokeWidth="2.4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  {f}
                </li>
              ))}
            </ul>

            <button
              className={plan.featured ? "btn-primary" : "btn-ghost"}
              type="button"
              disabled
              title="Billing is not connected yet"
            >
              Coming soon
            </button>
          </div>
        ))}
      </div>

      <p style={{ color: "var(--sa-faint)", fontSize: 12.5, marginTop: 24 }}>
        Questions about plans?{" "}
        <Link href="/dashboard/settings" style={{ color: "var(--sa-accent)" }}>
          See your current usage
        </Link>
        .
      </p>
    </>
  );
}
