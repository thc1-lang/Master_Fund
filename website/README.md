# Allen & Cooper — Corporate Website

A responsive corporate site for **allenandcooper.com**, built with Next.js App Router, TypeScript and Tailwind CSS 4.

## Pages
- `/` — home
- `/about` — firm and founder profiles
- `/research` — five research categories
- `/methodology` — research workflow
- `/insights` — three clearly labelled editorial sample notes
- `/contact` — enquiry form
- `/legal` — draft legal, research and privacy disclosures

Additional routes: `/api/contact` (POST), `/sitemap.xml`, `/robots.txt`.

## Quick start

Use Node.js 20.9+ (or a compatible newer LTS release).

```bash
npm install
cp .env.example .env.local  # on Windows: copy .env.example .env.local
npm run dev
```

Visit `http://localhost:3000`.

For a production check:

```bash
npm run build
npm run start
```

## Important: contact form

The contact form is **not connected to an email inbox out of the box**. It is intentionally fail-closed and returns an explicit error unless the following environment variables are configured:

```env
RESEND_API_KEY=re_...
CONTACT_TO_EMAIL=team@allenandcooper.com
CONTACT_FROM_EMAIL=Allen & Cooper <enquiries@allenandcooper.com>
```

Create an account with [Resend](https://resend.com), verify your sending domain and configure the required DNS records. Add those variables to Vercel Project Settings → Environment Variables. Avoid using `RESEND_API_KEY` in any `NEXT_PUBLIC_` variable. Verify delivery end-to-end before publishing the enquiry form.

The route validates fields, sanitises plain text, rejects a honeypot and uses a provider API. **Production hardening:** add persistent IP/user-based rate limiting, bot protection, monitoring and appropriate retention controls. Do not assume a serverless instance-local counter provides effective rate limiting.

## Content checklist before public launch

- [ ] Check legal entity name, registration number and registered office in `lib/site.ts`.
- [ ] Replace placeholder business email and founder biographies in `app/about/page.tsx` and `app/contact/page.tsx`.
- [ ] Review proposed wording against your actual business activity and evidence.
- [ ] Replace or approve editorial sample insights before publishing.
- [ ] Obtain UK financial-services legal advice on regulatory perimeter, investment research, possible financial promotions and required disclosures.
- [ ] Replace draft legal page with reviewed text, including genuine privacy details and verified third-party processors.
- [ ] Configure Resend credentials and test delivery.
- [ ] Confirm actual use of cookies/analytics and implement consent if required.
- [ ] Add production rate limiting/bot protection for public contact endpoint.
- [ ] Set `NEXT_PUBLIC_SITE_URL=https://allenandcooper.com`.
- [ ] Test phone, tablet and desktop layouts, keyboard navigation and screen-reader labels.

## Vercel deployment

1. Push this project to a private GitHub repository.
2. In Vercel: **Add New → Project**, import the repository.
3. Select **Next.js** (normally detected), set the environment variables in Project Settings, then deploy.
4. In **Settings → Domains**, add `allenandcooper.com` and `www.allenandcooper.com`.
5. Set `allenandcooper.com` as the primary domain and redirect `www` to it.
6. At your domain registrar, enter the **exact DNS records Vercel displays**. They can differ depending on setup. Preserve all existing email MX/SPF/DKIM/DMARC records.
7. Verify HTTPS and redirects, then test every page and form submission on the production domain.
8. Redeploy after any environment-variable changes, where needed.

Read the official documentation: https://vercel.com/docs/domains/working-with-domains/add-a-domain and https://nextjs.org/docs/app/getting-started/deploying.

**Note:** Domain ownership, registrar access and Vercel account access are required to complete publication. This project does not automatically alter your existing DNS or launch a public site.

## Design tokens

Core design values live in `app/globals.css`. The visual system uses midnight navy `#0d1b2a`, off-white `#f8f8f6`, muted gold `#c4a77d`, Georgia serif headlines and clean system sans-serif body copy. The hero chart is illustrative SVG artwork, not market performance data.

## Security and responsible publishing

- Never commit `.env.local` or secrets.
- Do not make claims of regulatory authorisation, historical investment performance or client relationships without supporting evidence.
- Legal text is deliberately labelled as draft; consult qualified advisers before publishing.
- Contact submissions may contain personal data; configure a lawful and documented retention policy.
