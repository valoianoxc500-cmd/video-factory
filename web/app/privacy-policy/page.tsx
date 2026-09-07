import type { Metadata } from "next";
import { LegalShell } from "@/components/LegalShell";
import { branding } from "@/lib/branding";

export const metadata: Metadata = {
  title: `Privacy Policy — ${branding.legalName}`,
  description: `How ${branding.legalName} collects, uses and stores your data.`,
};

const UPDATED = "5 September 2026";

export default function PrivacyPolicyPage() {
  const name = branding.legalName;

  return (
    <LegalShell title="Privacy Policy" updated={UPDATED}>
      <p>
        This policy explains what {name} collects when you use the service, why
        we collect it, where it is stored, and what you can do about it. It
        covers the {name} web application and the video generation it performs
        on your behalf.
      </p>

      <h2>Who we are</h2>
      <p>
        {name} is a service for producing short-form videos from a topic you
        supply. It researches the topic, writes a script, sources visuals,
        generates narration, and renders a finished video into your account&apos;s
        library. Questions about this policy can be sent to{" "}
        <a href={`mailto:${branding.contactEmail}`}>{branding.contactEmail}</a>.
      </p>

      <h2>Information we collect</h2>

      <h3>Account information</h3>
      <p>
        When you create an account we store your email address and the account
        identifier issued by our authentication provider. If you sign in with
        Google, Google returns your email address, basic profile information and
        a stable account identifier to us. We request only the{" "}
        <code>email</code> and <code>profile</code> scopes. We do not receive
        your Google password, and we cannot access your Gmail, Drive, contacts
        or any other Google service.
      </p>
      <p>
        If you register with an email address and password instead, the password
        is handled by our authentication provider and stored only as a hash. We
        never see or store your password in readable form.
      </p>

      <h3>Content you provide</h3>
      <p>
        We store the topics and settings you submit when you request a video,
        together with the status and progress of each generation job. This is
        what lets you see your work in progress and return to it later.
      </p>

      <h3>Content the service generates</h3>
      <p>
        Videos produced for you — along with their thumbnails, titles,
        descriptions and technical details such as duration and resolution — are
        stored in your account library so that they remain available after the
        job that created them has finished.
      </p>

      <h3>Technical information</h3>
      <p>
        Our hosting and infrastructure providers record standard operational
        data such as IP addresses, timestamps and error diagnostics. We use this
        to keep the service running and to investigate faults. We do not use
        advertising trackers, and we do not sell personal information.
      </p>

      <h2>Cookies</h2>
      <p>
        We set cookies that are necessary for the service to function: they keep
        you signed in between pages and protect the sign-in process. We do not
        use cookies for advertising or cross-site tracking. Clearing these
        cookies signs you out.
      </p>

      <h2>How we use your information</h2>
      <ul>
        <li>To create and secure your account, and to sign you in.</li>
        <li>To run the video generations you request and show their progress.</li>
        <li>To store your finished videos and show you your own library.</li>
        <li>To diagnose faults, prevent abuse, and keep the service available.</li>
        <li>To contact you about the service where necessary.</li>
      </ul>
      <p>
        We do not sell your personal information, and we do not use your content
        to advertise to you.
      </p>

      <h2>How your content is separated from other users</h2>
      <p>
        Every record is associated with the account that created it, and the
        database enforces that association on each request rather than relying
        on the application to filter correctly. Your videos and jobs are visible
        only to your own account.
      </p>

      <h2>Service providers</h2>
      <p>
        We rely on third parties to operate the service. They process data on
        our behalf, and each has its own privacy policy:
      </p>
      <ul>
        <li>
          <strong>Authentication and database</strong> — stores your account
          record, jobs and video metadata.
        </li>
        <li>
          <strong>Application hosting</strong> — serves the website and its API.
        </li>
        <li>
          <strong>Object storage</strong> — stores the rendered video and image
          files.
        </li>
        <li>
          <strong>Google</strong> — provides sign-in if you choose it, and
          supplies the AI models used for research, scripting, narration and
          speech timing.
        </li>
        <li>
          <strong>Web search and image sourcing providers</strong> — used to
          find source material for the topic you submit.
        </li>
      </ul>
      <p>
        Topics you submit, and text derived from them, are sent to these AI and
        search providers so that a video can be produced. Do not submit
        confidential or personal information in a topic.
      </p>

      <h2>Where data is stored</h2>
      <p>
        Data is stored on infrastructure operated by the providers above, which
        may be located in countries other than your own, including the United
        States. By using the service you understand that your information may be
        processed in those locations.
      </p>

      <h2>Retention</h2>
      <p>
        We keep your account information for as long as your account exists.
        Videos and their metadata remain in your library until you delete them
        or your account is closed. Job records may be removed once a generation
        has finished and its output has been saved.
      </p>

      <h2>Your choices</h2>
      <ul>
        <li>You can view the videos and jobs held in your account at any time.</li>
        <li>
          You can ask us to delete your account and its contents by writing to{" "}
          <a href={`mailto:${branding.contactEmail}`}>{branding.contactEmail}</a>.
        </li>
        <li>
          You can disconnect Google sign-in at any time from your Google
          Account&apos;s third-party access settings. Doing so prevents future
          sign-ins with Google.
        </li>
        <li>
          You can request a copy of the personal information we hold about you.
        </li>
      </ul>
      <p>
        Depending on where you live, you may have additional rights over your
        personal information. Contact us and we will do our best to help.
      </p>

      <h2>Security</h2>
      <p>
        Access to your data requires an authenticated session, credentials for
        our storage and infrastructure are held server-side and are never sent
        to the browser, and account separation is enforced in the database. No
        service can promise perfect security, and we make no certification or
        formal compliance claims in this policy.
      </p>

      <h2>Children</h2>
      <p>
        The service is not intended for children under 13, and we do not
        knowingly collect their personal information. If you believe a child has
        provided us with information, contact us and we will delete it.
      </p>

      <h2>Changes to this policy</h2>
      <p>
        We may update this policy as the service changes. The date at the top of
        this page shows when it was last revised. Continuing to use the service
        after a change means you accept the revised policy.
      </p>

      <h2>Contact</h2>
      <p>
        Questions, requests or complaints about this policy can be sent to{" "}
        <a href={`mailto:${branding.contactEmail}`}>{branding.contactEmail}</a>.
      </p>
    </LegalShell>
  );
}
