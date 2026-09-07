import type { Metadata } from "next";
import { LegalShell } from "@/components/LegalShell";
import { branding } from "@/lib/branding";

export const metadata: Metadata = {
  title: `Terms of Service — ${branding.legalName}`,
  description: `The terms governing your use of ${branding.legalName}.`,
};

const UPDATED = "5 September 2026";

export default function TermsPage() {
  const name = branding.legalName;

  return (
    <LegalShell title="Terms of Service" updated={UPDATED}>
      <p>
        These terms govern your use of {name}. By creating an account or using
        the service you agree to them. If you do not agree, please do not use
        the service.
      </p>

      <h2>The service</h2>
      <p>
        {name} produces short-form videos from a topic you supply. It researches
        the topic, writes a script, sources visuals, generates narration, and
        renders a finished video into your account library. The service is
        provided as-is and may change, and features may be added or withdrawn.
      </p>

      <h2>Your account</h2>
      <p>
        You need an account to use the service. You may register with an email
        address and password, or sign in with Google. You are responsible for
        keeping access to your account secure and for activity that happens
        under it. Tell us promptly if you believe your account has been used
        without your permission.
      </p>
      <p>
        You must be old enough to form a binding contract where you live, and at
        least 13 years old. Provide accurate registration information and keep
        it current.
      </p>

      <h2>Acceptable use</h2>
      <p>You agree not to use the service to:</p>
      <ul>
        <li>Break the law, or infringe anyone&apos;s copyright, trademark, privacy or other rights.</li>
        <li>
          Create material that harasses, defames, or impersonates a real person
          or organisation, or that presents fabricated events as genuine in a way
          intended to deceive.
        </li>
        <li>Produce sexual content involving minors, or content that promotes violence or unlawful harm.</li>
        <li>Generate misleading claims about identifiable people, health, or elections.</li>
        <li>
          Interfere with the service, work around its limits, or attempt to
          access another user&apos;s account or data.
        </li>
        <li>Resell or redistribute access to the service without our agreement.</li>
      </ul>
      <p>
        We may suspend or close an account that breaches these terms, and may
        remove content that does.
      </p>

      <h2>Your content</h2>
      <p>
        You keep ownership of the topics and instructions you submit. You grant
        us the permission needed to process them in order to run the service —
        including sending them to the AI and search providers described in the{" "}
        <a href="/privacy-policy">Privacy Policy</a> — and to store the resulting
        output in your library.
      </p>

      <h2>Generated videos</h2>
      <p>
        Subject to these terms, you may use the videos the service generates for
        you, including commercially. Please understand what that does and does
        not mean:
      </p>
      <ul>
        <li>
          <strong>Accuracy is not guaranteed.</strong> Videos are produced by
          automated systems and AI models. They can contain errors, omissions or
          statements that are simply wrong. Review a video before you publish or
          rely on it.
        </li>
        <li>
          <strong>Source material carries its own rights.</strong> The service
          may incorporate photographs, footage, music or other material obtained
          from third parties. You are responsible for confirming you have the
          rights you need before publishing or monetising a video.
        </li>
        <li>
          <strong>Similar output may exist.</strong> AI systems can produce
          comparable results for comparable inputs, so we cannot promise that
          any output is unique to you.
        </li>
        <li>
          <strong>You are responsible for what you publish.</strong> Where you
          distribute a video, the platform&apos;s own rules — including any
          requirement to disclose AI-generated content — apply to you.
        </li>
      </ul>

      <h2>Third-party services</h2>
      <p>
        The service depends on third parties for authentication, hosting,
        storage, AI models and search. Their availability affects ours, and your
        use of Google sign-in is also subject to Google&apos;s own terms.
      </p>

      <h2>Availability</h2>
      <p>
        We do not promise the service will be uninterrupted or error-free.
        Generation takes time, depends on external providers, and can fail. We
        may impose limits on usage to keep the service workable for everyone,
        and may suspend it for maintenance.
      </p>

      <h2>Disclaimers</h2>
      <p>
        To the fullest extent permitted by law, the service and everything it
        generates are provided &quot;as is&quot; and &quot;as available&quot;,
        without warranties of any kind, whether express or implied, including
        fitness for a particular purpose, non-infringement, or the accuracy of
        generated content.
      </p>

      <h2>Limitation of liability</h2>
      <p>
        To the fullest extent permitted by law, we are not liable for indirect,
        incidental, special or consequential losses, or for lost profits, lost
        revenue, or lost or corrupted data, arising from your use of the service.
        Nothing in these terms excludes liability that cannot be excluded by law.
      </p>

      <h2>Indemnity</h2>
      <p>
        You agree to indemnify us against claims arising from your use of the
        service, the content you submit, or videos you publish, where those
        claims result from your breach of these terms or of the law.
      </p>

      <h2>Ending your use</h2>
      <p>
        You may stop using the service at any time and ask us to delete your
        account by writing to{" "}
        <a href={`mailto:${branding.contactEmail}`}>{branding.contactEmail}</a>.
        We may suspend or end access if you breach these terms or if we stop
        offering the service. When an account is closed, its stored videos and
        records may be deleted.
      </p>

      <h2>Changes to these terms</h2>
      <p>
        We may revise these terms as the service develops. The date at the top of
        this page shows when they were last changed. Continuing to use the
        service after a change means you accept the revised terms.
      </p>

      <h2>Contact</h2>
      <p>
        Questions about these terms can be sent to{" "}
        <a href={`mailto:${branding.contactEmail}`}>{branding.contactEmail}</a>.
      </p>
    </LegalShell>
  );
}
