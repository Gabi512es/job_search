import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

export const Route = createFileRoute("/_authenticated/onboarding")({
  head: () => ({
    meta: [
      { title: "Votre profil — Recherche d'emploi assistée par IA" },
      {
        name: "description",
        content: "Déposez votre CV et précisez les postes et lieux qui vous intéressent.",
      },
      { property: "og:title", content: "Votre profil" },
      { property: "og:description", content: "Déposez votre CV et précisez vos critères." },
    ],
  }),
  component: Onboarding,
});

const WORK_TYPES = [
  { value: "remote", label: "À distance" },
  { value: "hybrid", label: "Hybride" },
  { value: "onsite", label: "Sur site" },
  { value: "any", label: "Peu importe" },
];

function Onboarding() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [userId, setUserId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  // step 1
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [cvPath, setCvPath] = useState<string | null>(null);

  // step 2
  const [searchMode, setSearchMode] = useState<"precise" | "exploration" | null>(null);
  const [titles, setTitles] = useState<string[]>([""]);
  const [location, setLocation] = useState("");
  const [workTypes, setWorkTypes] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getUser();
      const user = data.user;
      if (!user) return;
      setUserId(user.id);

      const { data: existing } = await supabase
        .from("profiles")
        .select("*")
        .eq("user_id", user.id)
        .maybeSingle();

      if (!existing) {
        await supabase.from("profiles").insert({
          user_id: user.id,
          email: user.email ?? null,
          full_name: (user.user_metadata?.["full_name"] as string) ?? null,
          avatar_url: (user.user_metadata?.["avatar_url"] as string) ?? null,
        });
      } else {
        if (existing.cv_storage_path) setCvPath(existing.cv_storage_path);
        if (existing.search_mode) setSearchMode(existing.search_mode as "precise" | "exploration");
        if (existing.target_titles?.length) setTitles(existing.target_titles);
        if (existing.target_location) setLocation(existing.target_location);
        if (existing.work_types?.length) setWorkTypes(existing.work_types);
        if (existing.onboarding_completed_at) setStep(2);
      }
      setReady(true);

    })();
  }, []);

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file || !userId) {
      toast.error("Choisissez d'abord un fichier.");
      return;
    }
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "pdf";
    if (!["pdf", "txt"].includes(ext)) {
      toast.error("Formats acceptés : PDF ou texte.");
      return;
    }
    setUploading(true);
    const path = `${userId}/cv.${ext}`;
    const { error } = await supabase.storage
      .from("resumes")
      .upload(path, file, { upsert: true });

    if (error) {
      setUploading(false);
      toast.error("L'envoi a échoué. Merci de réessayer.");
      return;
    }

    const { error: dbError } = await supabase
      .from("profiles")
      .update({ cv_storage_path: path })
      .eq("user_id", userId);

    setUploading(false);
    if (dbError) {
      toast.error("Le fichier est enregistré mais votre profil n'a pas pu être mis à jour.");
      return;
    }
    setCvPath(path);
    toast.success("CV enregistré.");
    setStep(1);
  };

  const toggleWorkType = (value: string) => {
    setWorkTypes((prev) => {
      if (value === "any") return prev.includes("any") ? [] : ["any"];
      const next = prev.filter((v) => v !== "any");
      return next.includes(value) ? next.filter((v) => v !== value) : [...next, value];
    });
  };

  const cleanTitles = titles.map((t) => t.trim()).filter(Boolean);

  const canSubmit =
    !!searchMode &&
    (searchMode === "exploration" || cleanTitles.length > 0) &&
    location.trim().length > 0 &&
    workTypes.length > 0;

  const handleSubmit = async () => {
    if (!userId || !canSubmit) return;
    setSaving(true);
    const storedWorkTypes = workTypes.includes("any")
      ? ["remote", "hybrid", "onsite"]
      : workTypes;

    const { error } = await supabase
      .from("profiles")
      .update({
        search_mode: searchMode,
        target_titles: searchMode === "precise" ? cleanTitles : [],
        target_location: location.trim(),
        work_types: storedWorkTypes,
        raw_answers: {
          search_mode: searchMode,
          target_titles: cleanTitles,
          target_location: location.trim(),
          work_types_selected: workTypes,
        },
        onboarding_completed_at: new Date().toISOString(),
      })
      .eq("user_id", userId);

    setSaving(false);
    if (error) {
      toast.error("Vos réponses n'ont pas pu être enregistrées.");
      return;
    }
    setStep(2);
  };

  const handleSignOut = async () => {
    await supabase.auth.signOut();
    navigate({ to: "/", replace: true });
  };

  if (!ready) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Chargement…</p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-xl flex-col justify-center gap-6 px-4 py-12">
      <Progress value={((step + 1) / 3) * 100} />

      {step === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Votre CV</CardTitle>
            <CardDescription>Ajoutez votre CV au format PDF ou texte.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Input ref={fileRef} type="file" accept=".pdf,.txt,application/pdf,text/plain" />
            {cvPath && (
              <p className="text-sm text-muted-foreground">Un CV est déjà enregistré.</p>
            )}
            <div className="flex gap-2">
              <Button onClick={handleUpload} disabled={uploading}>
                {uploading ? "Envoi…" : "Envoyer mon CV"}
              </Button>
              {cvPath && (
                <Button variant="outline" onClick={() => setStep(1)}>
                  Continuer
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {step === 1 && (
        <Card>
          <CardHeader>
            <CardTitle>Ce que vous recherchez</CardTitle>
            <CardDescription>Trois questions rapides pour cadrer la recherche.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-3">
              <Label>
                Avez-vous un ou plusieurs postes précis en tête, ou préférez-vous explorer large à
                partir de votre CV ?
              </Label>
              <div className="grid gap-2 sm:grid-cols-2">
                <Button
                  variant={searchMode === "precise" ? "default" : "outline"}
                  onClick={() => setSearchMode("precise")}
                >
                  J'ai des postes précis en tête
                </Button>
                <Button
                  variant={searchMode === "exploration" ? "default" : "outline"}
                  onClick={() => setSearchMode("exploration")}
                >
                  Je préfère explorer largement
                </Button>
              </div>
            </div>

            {searchMode === "precise" && (
              <div className="space-y-3">
                <Label>Quels intitulés de poste recherchez-vous ?</Label>
                {titles.map((title, index) => (
                  <div key={index} className="flex gap-2">
                    <Input
                      value={title}
                      placeholder="ex. Chef de projet digital"
                      onChange={(e) =>
                        setTitles((prev) =>
                          prev.map((t, i) => (i === index ? e.target.value : t)),
                        )
                      }
                    />
                    {titles.length > 1 && (
                      <Button
                        variant="ghost"
                        onClick={() => setTitles((prev) => prev.filter((_, i) => i !== index))}
                      >
                        Retirer
                      </Button>
                    )}
                  </div>
                ))}
                {titles.length < 5 && (
                  <Button variant="outline" onClick={() => setTitles((prev) => [...prev, ""])}>
                    Ajouter un intitulé
                  </Button>
                )}
              </div>
            )}

            <div className="space-y-3">
              <Label htmlFor="location">Où cherchez-vous ?</Label>
              <Input
                id="location"
                value={location}
                placeholder="Ville ou région"
                onChange={(e) => setLocation(e.target.value)}
              />
              <div className="grid gap-2 sm:grid-cols-2">
                {WORK_TYPES.map((type) => (
                  <label
                    key={type.value}
                    className="flex items-center gap-2 rounded-md border border-border p-3 text-sm"
                  >
                    <Checkbox
                      checked={workTypes.includes(type.value)}
                      onCheckedChange={() => toggleWorkType(type.value)}
                    />
                    {type.label}
                  </label>
                ))}
              </div>
            </div>

            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setStep(0)}>
                Retour
              </Button>
              <Button onClick={handleSubmit} disabled={!canSubmit || saving}>
                {saving ? "Enregistrement…" : "Terminer"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {step === 2 && (
        <Card>
          <CardHeader>
            <CardTitle>Merci, on prépare votre recherche</CardTitle>
            <CardDescription>
              Votre CV et vos critères sont enregistrés. Nous vous préviendrons dès que les
              premières offres sélectionnées seront prêtes.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex gap-2">
            <Button variant="outline" onClick={() => setStep(1)}>
              Modifier mes réponses
            </Button>
            <Button variant="ghost" onClick={handleSignOut}>
              Se déconnecter
            </Button>
          </CardContent>
        </Card>
      )}
    </main>
  );
}
