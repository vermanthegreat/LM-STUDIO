import sys
import json

def sortiraj(ulazni_fajl, izlazni_fajl, po_cemu="kompanija"):
    with open(ulazni_fajl, 'r', encoding='utf-8') as f:
        linije = f.readlines()
    # Primjer sortiranja po prvoj riječi u liniji (pretpostavlja format: Ime, Email, Kompanija)
    linije.sort(key=lambda x: x.split(',')[2].strip() if len(x.split(','))>2 else x)
    with open(izlazni_fajl, 'w', encoding='utf-8') as f:
        f.writelines(linije)
    return f"Sortirano {len(linije)} redova u {izlazni_fajl}"

if __name__ == "__main__":
    # Očekuje argumente: python sort_kontakti.py ulaz.txt izlaz.txt
    if len(sys.argv) >= 3:
        rez = sortiraj(sys.argv[1], sys.argv[2])
        print(rez)
    else:
        print("Greška: nedostaju argumenti")