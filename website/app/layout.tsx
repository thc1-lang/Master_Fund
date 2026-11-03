import type { Metadata, Viewport } from "next";
import { Header } from "@/components/header";
import { Footer } from "@/components/footer";
import { site } from "@/lib/site";
import "./globals.css";
export const metadata: Metadata = {
  metadataBase: new URL(site.url),
  title: { default: "Allen & Cooper | Independent Investment Research", template: "%s | Allen & Cooper" },
  description: site.description,
  openGraph: { title: "Allen & Cooper", description: site.description, type: "website", url: site.url },
  robots: { index:true, follow:true },
};
export const viewport: Viewport = { width: "device-width", initialScale: 1 };
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="en"><body><a href="#main" className="skip-link">Skip to content</a><Header/><main id="main">{children}</main><Footer/></body></html>}
