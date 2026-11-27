# RPM balíček pro openSUSE a další distribuce používající RPM.
# Hodnoty předává workflow přes --define:
#   app_version  verze (např. 1.2.3)
#   stage_dir    adresář dist/vlcr-kancelar
#   icons_dir    adresář s ikonami vlcr-*.png

%global debug_package %{nil}
%global _build_id_links none
%define __os_install_post %{nil}
%define _unpackaged_files_terminate_build 0
%define _missing_doc_files_terminate_build 0

Name:           vlcr-kancelar
Version:        %{app_version}
Release:        1
Summary:        Kancelářský balík VLČR Kancelář
License:        Proprietary
Group:          Productivity/Office/Suite
Packager:       Vzdělaný lid ČR <info@vlcr.cz>
BuildArch:      x86_64
AutoReqProv:    no
Recommends:     libGL1
Recommends:     libxkbcommon0
Recommends:     libxkbcommon-x11-0
Recommends:     libxcb-icccm4
Recommends:     libxcb-image0
Recommends:     libxcb-keysyms1
Recommends:     libxcb-randr0
Recommends:     libxcb-render-util0
Recommends:     libxcb-shape0
Recommends:     libxcb-xinerama0
Recommends:     libxcb-xkb1

%description
VLČR Kancelář je kancelářský balík obsahující Písař, Počítář, Prezentář,
Průzkumník souborů a Poscilogramář pro tvorbu digitálních oscilogramů.

%prep
# Obsah se bere z hotového adresáře stage_dir.

%build
# Programy jsou již sestaveny pomocí PyInstalleru.

%install
rm -rf %{buildroot}
install -d %{buildroot}/opt/vlcr-kancelar
cp -a %{stage_dir}/. %{buildroot}/opt/vlcr-kancelar/

install -d %{buildroot}%{_bindir}
ln -s /opt/vlcr-kancelar/vlcr-kancelar %{buildroot}%{_bindir}/vlcr-kancelar
for app in pisar pocitar prezentar pruzkumnik poscilogramar; do
    test -x "%{buildroot}/opt/vlcr-kancelar/$app" || {
        echo "CHYBÍ sestavená aplikace: $app" >&2
        exit 1
    }
    ln -s "/opt/vlcr-kancelar/$app" "%{buildroot}%{_bindir}/vlcr-$app"
done

install -d %{buildroot}%{_datadir}/pixmaps
install -m 0644 %{icons_dir}/vlcr-*.png %{buildroot}%{_datadir}/pixmaps/

install -d %{buildroot}%{_datadir}/applications
mk_desktop() {
cat > "%{buildroot}%{_datadir}/applications/$1.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=$4
Icon=$1
Terminal=false
Categories=$5
EOF
}
mk_desktop vlcr-kancelar       "VLČR Kancelář"      "Hlavní rozcestník sady VLČR Kancelář" "/opt/vlcr-kancelar/vlcr-kancelar"       "Office;"
mk_desktop vlcr-pisar          "Písař"              "Textový editor"                       "/opt/vlcr-kancelar/pisar"               "Office;WordProcessor;"
mk_desktop vlcr-pocitar        "Počítář"            "Tabulkový procesor"                   "/opt/vlcr-kancelar/pocitar"             "Office;Spreadsheet;"
mk_desktop vlcr-prezentar      "Prezentář"          "Editor prezentací"                    "/opt/vlcr-kancelar/prezentar"           "Office;Presentation;"
mk_desktop vlcr-pruzkumnik     "Průzkumník souborů" "Správa souborů .vlcr"                 "/opt/vlcr-kancelar/pruzkumnik"          "System;FileManager;"
mk_desktop vlcr-poscilogramar  "Poscilogramář"      "Vytváření digitálních oscilogramů"    "/opt/vlcr-kancelar/poscilogramar"       "Education;Science;"

%post
update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :

%postun
update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :

%files
%defattr(-,root,root,-)
/opt/vlcr-kancelar
%{_bindir}/vlcr-kancelar
%{_bindir}/vlcr-pisar
%{_bindir}/vlcr-pocitar
%{_bindir}/vlcr-prezentar
%{_bindir}/vlcr-pruzkumnik
%{_bindir}/vlcr-poscilogramar
%{_datadir}/applications/vlcr-*.desktop
%{_datadir}/pixmaps/vlcr-*.png

%changelog
* Fri Nov 13 2026 Vzdělaný lid ČR <info@vlcr.cz> - %{version}-1
- Přidán Poscilogramář do společného balíčku
