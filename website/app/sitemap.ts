import type { MetadataRoute } from "next";
import { site } from "@/lib/site";
export default function sitemap():MetadataRoute.Sitemap{return ["","/about","/research","/methodology","/insights","/contact","/legal"].map(p=>({url:`${site.url}${p}`,lastModified:new Date(),changeFrequency:"monthly" as const,priority:p===""?1:0.6}));}
