import os 
from astropy.io import fits
import numpy as np
from astropy.table import Table, join
import h5py
import healpy as hp
from multiprocessing import Pool
from tqdm import tqdm
import subprocess

_healpix_nside = 16

PIPELINES = ['TGLC']

# To-Do:
# - Scripts for downloading the .sh files and .csv from TGLC MAST
# - Cross-matching across sectors with gaia_id 
# - Write function to create .sh files


def read_sh(fp: str):
    '''
    read_sh reads an sh file, parses and returns the curls commands from the file
    
    Parameters
    ----------
    fp: str, path to the .sh file
    
    Returns
    ------- 
    lines: list, list of curl commands in the .sh file for downloading a single light curve
    '''

    with open(fp, 'r') as f:
        lines = f.readlines()[1:]
    return lines

def parse_line(line: str):
    '''
    Parse a line from the .sh file, extract the relevant parts (gaia_id, cam, ccd, fp1, fp2, fp3, fp4) and return them as a list

    Parameters
    ----------
    line: str, a line from the .sh file
    
    Returns
    ------- 
    params: list, list of parameters extracted from the line
    '''

    parts = line.split()
    output_path = parts[4].strip("'")
        
    # Split the path and extract the relevant parts
    path_parts = output_path.split('/')
    
    cam = int(path_parts[1].split('-')[0].split('cam')[1])
    ccd = int(path_parts[1].split('-')[1].split('ccd')[1])
    numbers = path_parts[2:6]
    gaia_id = path_parts[-1].split('-')[1]

    return [int(gaia_id), cam, ccd, *numbers]

def lc_path(lc_fits_dir, sector_str, args):
    '''
    Construct the path to the light curve file given the parameters

    Parameters
    ----------
    lc_fits_dir: str, path to the directory containing the light curve files
    sector_str: str, sector string
    args: list, list of parameters extracted from the line
    
    Returns
    ------- 
    path: str, path to the light curve file
    '''

    return os.path.join(lc_fits_dir, f'cam{args["cam"]}-ccd{args["ccd"]}/{args["fp1"]}/{args["fp2"]}/{args["fp3"]}/{args["fp4"]}/hlsp_tglc_tess_ffi_gaiaid-{args["gaiadr3_id"]}-{sector_str}-cam{args["cam"]}-ccd{args["ccd"]}_tess_v1_llc.fits')

def parse_curl_commands(sh_file):
    '''
    Parse the curl commands from the .sh file

    Parameters
    ----------
    sh_file: str, path to the .sh file
    
    Returns
    ------- 
    params: list, list of parameters extracted from the lines
    '''

    lines = read_sh(sh_file)
    params = list(parse_line(line) for line in lines)
    return params 

def create_sector_catalog(sector: int, tess_data_path: str, tiny: bool = True):
    '''
    Create a sector catalog from the .sh file. Sector catalogs contains: gaiadr3_id, cam, ccd, fp1, fp2, fp3, fp4 
    for each light curve in the sector.

    Parameters
    ----------
    sector: int, sector number
    tess_data_path: str, path to the directory containing the light curve fits files
    tiny: bool, if True, only use a small sample for testing
    Returns
    ------- 
    catalog: astropy Table, sector catalog
    '''

    sector_str = f's{sector:04d}'
    sh_fp = os.path.join(tess_data_path, f'{sector_str}/fits/hlsp_tglc_tess_ffi_{sector_str}_tess_v1_llc.sh')
    params = parse_curl_commands(sh_fp)
    column_names = ['gaiadr3_id', 'cam', 'ccd', 'fp1', 'fp2', 'fp3', 'fp4']
    catalog = Table(rows=params, names=column_names)
    if tiny:
        catalog = catalog[0:20]

    return catalog

def get_fits_lightcurve(curl_cmd, output_fp):
    '''
    Download the light curve file using the curl command and save it to the output file

    Parameters
    ----------
    curl_cmd: str, curl command
    output_fp: str, path to the output file
    
    Returns
    ------- 
    success: bool, True if the download was successful, False otherwise
    '''

    try:
        subprocess.run(curl_cmd, shell=True, check=True, text=True, capture_output=True)
        return True, f"Successfully downloaded: {output_fp}"
    except subprocess.CalledProcessError as e:
        return False, f"Error downloading using the following cmd: {curl_cmd}: {e.stderr}"
 
def processing_fn(
        row
):
    ''' 
    Process a single light curve file into the standard format 

    Parameters
    ----------
    row: astropy Row, a single row from the sector catalog containing the object descriptors: gaiadr3_id, cam, ccd, fp1, fp2, fp3, fp4
    
    Returns
    ------- 
    lightcurve: dict, light curve in the standard format
    i.e. 
        {
            'TIC_ID': tess id,
            'gaiadr3_id': gaia id,
            'time': obs_times: arr_like,
            'psf_flux': psf_fluxes: arr_like,
            'psf_flux_err': psf_flux_err: float,
            'aper_flux': aperture_fluxes: arr_like,
            'aper_flux_err': aperture_flux_err: float,
            'tess_flags': tess_flags: arr_like,
            'tglc_flags': tglc_flags: arr_like,
            'RA': ra: float,
            'DEC': dec # More columns maybe required...
        }
    '''
    sector_str = f's{23:04d}' # remove hardcode - need to make this dynamic by including the sector as an argument

    fits_path = f'./{sector_str}/cam{row["cam"]}-ccd{row["ccd"]}/{row["fp1"]}/{row["fp2"]}/{row["fp3"]}/{row["fp4"]}/hlsp_tglc_tess_ffi_gaiaid-{row["gaiadr3_id"]}-s0023-cam{row["cam"]}-ccd{row["cam"]}_tess_v1_llc.fits'

    try:
        with fits.open(fits_path, mode='readonly', memmap=True) as hdu:
            # Filter by TESS quality flags - TO-DO

            return {'TIC_ID': row['TIC_ID'],
                    'gaiadr3_id': row['gaiadr3_id'],
                    'time': hdu[1].data['time'],
                    'psf_flux': hdu[1].data['psf_flux'],
                    'psf_flux_err': hdu[1].header['psf_err'],
                    'aper_flux': hdu[1].data['aperture_flux'],
                    'aper_flux_err': hdu[1].header['aper_err'],
                    'tess_flags': hdu[1].data['TESS_flags'],
                    'tglc_flags': hdu[1].data['TGLC_flags'],
                    'RA': hdu[1].header['ra_obj'],
                    'DEC': hdu[1].header['dec_obj']
                    }
        
    except FileNotFoundError:
        print(f"File not found: {fits_path}")
        return

def save_in_standard_format(args):
    '''
    Save the standardised light curve dict in a hdf5 file 

    Parameters
    ----------
    args: tuple, tuple of arguments: (catalog, output_filename, tess_data_path)

    Returns
    -------
    success: bool, True if the file was saved successfully, False otherwise
    '''

    catalog, output_filename, tess_data_path = args
    
    if not os.path.exists(os.path.dirname(output_filename)):
        os.makedirs(os.path.dirname(output_filename))

    results = []
    # parallelize this
    for row in catalog:
        results.append(processing_fn(row))

    max_length = max([len(d['time']) for d in results])

    for i in range(len(results)):
        for key in results[i].keys():
            if isinstance(results[i][key], np.ndarray):
                results[i][key] = np.pad(results[i][key], (0,max_length - len(results[i][key])), mode='constant')

    lightcurves = Table({k: [d[k] for d in results]
                     for k in results[0].keys()})
    lightcurves.convert_unicode_to_bytestring()

    with h5py.File(output_filename, 'w') as hdf5_file:
        for key in lightcurves.colnames:
            hdf5_file.create_dataset(key, data=lightcurves[key])
    return 1


def main(output_dir = './test', tess_data_path = './tess_data', tiny = True, n_processes = 4):
    
    sector = 23
    sector_str = f's{sector:04d}'

    # Download the sh file from the TGLC site
    
    catalog = create_sector_catalog(23, tess_data_path = "./tess_data", tiny = tiny)

    lcs = Table.read(os.path.join(tess_data_path, "./s0023/s0023.csv")) # Do this for all sectors
    lcs.rename_column('#GAIADR3_ID', 'gaiadr3_id')

    lcs['healpix'] = hp.ang2pix(_healpix_nside, lcs['RA'], lcs['DEC'], lonlat=True, nest=True)
   
    # Join the catalogs using the gaia_id
    catalog = join(catalog, lcs, keys='gaiadr3_id', join_type='inner')
    #catalog.write(os.path.join(tess_data_path, f'{sector_str}', f'{sector_str}-catalog.hdf5'), format='hdf5', overwrite=True, path=os.path.join(tess_data_path, f'{sector_str}', f'{sector_str}-catalog.hdf5'))

    catalog = catalog[0:20] # use small sample for testing 
    output_fp = os.path.join(tess_data_path, f'{sector_str}', f'lcs') # output filepath of the fits light curves
    
    #parallel_args = [(curl_cmd, output_fp) for curl_cmd in read_sh(os.path.join(tess_data_path, f'{sector_str}', f'hlsp_tglc_tess_ffi_{sector_str}_tess_v1_llc.sh'))]
    #with Pool(n_processes) as pool: # adjust the number of avaialble cores depending on CPU allocation
        
        #results = list(tqdm(pool.starmap(get_fits_lightcurve, parallel_args), total=len(parallel_args)))
    
    #successful = [r for r, _ in results if r]
    #print(f"Successfully downloaded {len(successful)} out of {len(catalog)} files.")
    #print(len(parallel_args))

    # Now lets convert the fits light curves to the standard format
    output_dir = f'./tess_data/{sector_str}/lcs' # rename

    catalog = catalog.group_by(['healpix'])

    map_args = []

    for group in catalog.groups: 
        print(len(group), group['healpix'][0])
        # Create a filename for the group
        group_filename = os.path.join(output_dir, '{}/healpix={}/001-of-001.hdf5'.format("TGLC", group['healpix'][0]))
        map_args.append((group, group_filename, tess_data_path))

    with Pool(n_processes) as pool:
        results = list(tqdm(pool.imap(save_in_standard_format, map_args), total=len(map_args)))
    
    if sum(results) != len(map_args):
        print("There was an error in the parallel processing, some files may not have been processed correctly")

    print("All done!")
    return 

if __name__ == '__main__':
    main(output_dir = './test', tess_data_path = './tess_data', tiny = False)