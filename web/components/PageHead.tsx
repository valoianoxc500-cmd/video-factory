import type { ReactNode } from "react";

/**
 * A section heading with its own cinematic still behind it.
 *
 * One component so the treatment is identical on every surface: same veil,
 * same crop, same contrast floor. `art` names a file in /public/channels;
 * omit it and the heading renders plain, which is what the legal and auth
 * pages want.
 *
 * The images are licensed from Pexels and served from our own origin — see
 * public/channels/CREDITS.txt.
 */
export function PageHead({
  title,
  sub,
  art,
  children,
}: {
  title: ReactNode;
  sub?: ReactNode;
  /** Basename in /public/channels, without the extension. */
  art?: string;
  children?: ReactNode;
}) {
  const styled = art
    ? {
        "data-art": art,
        style: { ["--head-art" as string]: `url('/channels/${art}.jpg')` },
      }
    : {};

  return (
    <div className="page-head" {...styled}>
      <h1>{title}</h1>
      {sub && <p>{sub}</p>}
      {children}
    </div>
  );
}
