import os 
from astropy.io import fits
import numpy as np
from astropy.table import Table, join, row
import h5py
import healpy as hp
from multiprocessing import Pool
from tqdm import tqdm
import subprocess
from datasets import load_dataset
from datasets.data_files import DataFilesPatternsDict

import argparse
import time

_healpix_nside = 16
_TINY_SIZE = 100 # Number of light curves to use for testing.
_BATCH_SIZE = 5000 # number of light curves requests to submit to MAST at a time. These are processed in parallel.
PAUSE_TIME = 3 # Pause time between retries to MAST server

# TODO:
# Specify pipeline with pipeline flag: this will choose the URL for downloads
# In the inherited class - implement the correct data cleaning procedures.
# TESS-SPOC: https://archive.stsci.edu/hlsp/tess-spoc 


PIPELINES = ['QLP']

class QLP_Downloader:
    '''
    A helper class for downloading and processing QLP lightcurve data.

    Parameters
    ----------
    sector: int, 
        The TESS sector number.
    qlp_data_path: str, 
        Path to the directory containing the QLP data.
    hdf5_output_dir: str, 
        Path to the directory to save the hdf5 files
    fits_dir: str, 
        Path to the directory to save the fits files
    n_processes: int, 
        Number of processes to use for parallel processing

    Attributes
    ----------
    sector: int, 
        The TESS sector number.
    sector_str: str, 
        The TESS sector string.
    qlp_data_path: str, 
        Path to the directory containing the QLP data.
    hdf5_output_dir: str, 
        Path to the directory to save the hdf5 files
    fits_dir: str, 
        Path to the directory to save the fits files
    n_processes: int, 
        Number of processes to use for parallel processing

    Methods
    -------
    read_sh(fp: str)
        Read an sh file and return the curl commands
    parse_line(line: str)
        Parse a line from the sh file and return the relevant parameters
    lc_path(lc_fits_dir, args)
        Construct the path to the light curve file given the parameters
    parse_curl_commands(sh_file)
        Parse the curl commands from the sh file
    create_sector_catalog(save_catalog: bool = False, tiny: bool = True)
        Create a sector catalog from the .sh file
    get_fits_lightcurve(catalog_row)
        Download the light curve file using the curl command and save it to the output file
    processing_fn(row)
        Process a single light curve file into the standard format
    save_in_standard_format(catalog, filename)
        Save the standardised batch of light curves dict in a hdf5 file

    Examples    
    --------    
    >>> downloader = QLP_Downloader(sector=23, qlp_data_path='./qlp', hdf5_output_dir='./qlp', fits_dir='./qlp/fits', n_processes=4)
    >>> catalog = downloader.create_sector_catalog(save_catalog=True, tiny=True)
    >>> downloader.get_fits_lightcurve(catalog)
    >>> downloader.processing_fn(catalog[idx])
    >>> downloader.save_in_standard_format(catalog, filename)
    '''
    def __init__(self, sector: int, qlp_data_path: str, hdf5_output_dir: str, fits_dir: str, n_processes: int = 1):
        self.sector = sector
        self.sector_str = f's{sector:04d}'
        self.qlp_data_path = qlp_data_path
        self.hdf5_output_dir = hdf5_output_dir
        self.fits_dir = fits_dir
        self.n_processes = n_processes

    def __repr__(self) -> str:
        return f"QLP_Downloader(sector={self.sector}, qlp_data_path={self.qlp_data_path}, hdf5_output_dir={self.hdf5_output_dir}, fits_dir={self.fits_dir}, n_processes={self.n_processes})"

    def read_sh(self, fp: str) -> list[str]:
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

    def parse_line(self, line: str) -> list[int]:
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
        numbers = path_parts[1:5]
        TIC_ID = path_parts[-1].split('-')[1].split('_')[0]

        return [int(TIC_ID), *numbers]

    def lc_path(self, lc_fits_dir: str, args: dict[str]) -> str:
        '''
        Construct the path to the light curve file given the parameters

        Parameters
        ----------
        lc_fits_dir: str, path to the directory containing the light curve files
        args: list, list of parameters extracted from the line
        
        Returns
        ------- 
        path: str, path to the light curve file
        '''

        return os.path.join(lc_fits_dir, f'cam{args["cam"]}-ccd{args["ccd"]}/{args["fp1"]}/{args["fp2"]}/{args["fp3"]}/{args["fp4"]}/hlsp_qlp_tess_ffi_gaiaid-{args["gaiadr3_id"]}-{self.sector_str}-cam{args["cam"]}-ccd{args["ccd"]}_tess_v1_llc.fits')

    def parse_curl_commands(self, sh_fp: str) -> list[list[int]]:
        '''
        Parse the curl commands from the .sh file

        Parameters
        ----------
        sh_file: str, path to the .sh file
        
        Returns
        ------- 
        params: list, list of parameters extracted from the lines
        '''

        lines = self.read_sh(sh_fp)
        params = list(self.parse_line(line) for line in lines)
        return params 

    def create_sector_catalog(self, save_catalog: bool = False, tiny: bool = True) -> Table:
        '''
        Create a sector catalog from the .sh file. Sector catalogs contains: gaiadr3_id, cam, ccd, fp1, fp2, fp3, fp4 
        for each light curve in the sector.

        Parameters
        ----------
        tiny: bool, if True, only use a small sample of 100 objects for testing

        Returns
        ------- 
        catalog: astropy Table, sector catalog
        '''

        sh_fp = os.path.join(self.qlp_data_path, f'{self.sector_str}_fits_download_script.sh')
        params = self.parse_curl_commands(sh_fp)
        column_names = ['TIC_ID', 'fp1', 'fp2', 'fp3', 'fp4']
        catalog = Table(rows=params, names=column_names)

        if tiny:
            catalog = catalog[0:_TINY_SIZE]

        # Merge with target list to get RA-DEC
        csv_fp = os.path.join(self.qlp_data_path, f'{self.sector_str}_target_list.csv')
        target_list = Table.read(csv_fp, format='csv')
        target_list.rename_column('#TIC_ID', 'TIC_ID')

        catalog = join(catalog, target_list, keys='TIC_ID', join_type='inner') # remove duplicates from qlp

        catalog['healpix'] = hp.ang2pix(_healpix_nside, catalog['RA'], catalog['DEC'], lonlat=True, nest=True)

        if save_catalog:
            output_fp = os.path.join(self.qlp_data_path, f'{self.sector_str}_catalog{"_tiny" if tiny else ""}.hdf5')
            catalog.write(output_fp, format='hdf5', overwrite=True, path=output_fp)
            print(f"Saved catalog to {output_fp}")
        return catalog

    def get_fits_lightcurve(self, catalog_row: row.Row) -> bool: # catalog_row : type
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

        path = f'{self.sector_str}/{catalog_row["fp1"]}/{catalog_row["fp2"]}/{catalog_row["fp3"]}/{catalog_row["fp4"]}/hlsp_qlp_tess_ffi_{self.sector_str}-{f'{catalog_row["TIC_ID"]:016d}'}_tess_v01_llc.fits' #f'cam{catalog_row["cam"]}-ccd{catalog_row["ccd"]}/{catalog_row["fp1"]}/{catalog_row["fp2"]}/{catalog_row["fp3"]}/{catalog_row["fp4"]}/hlsp_qlp_tess_ffi_gaiaid-{catalog_row["gaiadr3_id"]}-{self.sector_str}-cam{catalog_row["cam"]}-ccd{catalog_row["ccd"]}_tess_v1_llc.fits'
        #url = f'https://archive.stsci.edu/hlsps/tglc/{self.sector_str}/' + path 

        url = f'https://mast.stsci.edu/api/v0.1/Download/file/?uri=mast:HLSP/qlp/' + path

        #output_fp = os.path.join(self.fits_dir, path)
        # Create output directory if it doesn't exist - get rid this is SLOW
        # add the make dir in the wget 
        #os.makedirs(os.path.dirname(output_fp), exist_ok=True)

        cmd = f'curl {url} --create-dirs -o {os.path.join(self.fits_dir, path)}'

        try:
            subprocess.run(cmd, shell=True, check=True, text=True, capture_output=True)
            return True #, f"Successfully downloaded: {output_fp}"
        
        except subprocess.CalledProcessError as e:
            return False #, f"Error downloading using the following cmd: {cmd}: {e.stderr}"

    def processing_fn(
            self,
            catalog_row : row.Row
    ) -> dict:
        ''' 
        Process a single light curve file into the standard format 

        Parameters
        ----------
        catalog_row: astropy Row, a single row from the sector catalog containing the object descriptors: gaiadr3_id, cam, ccd, fp1, fp2, fp3, fp4
        
        Returns
        ------- 
        lightcurve: dict, light curve in the standard format
        i.e. 
            {
                'TIC_ID': id: str,
                'time': times: arr_like,
                'sap_flux': simple aperture fluxes: arr_like,
                'kspsap_flux': KSP aperture fluxes: arr_like,
                'kspsap_flux_err': KSP aperture fluxes errors: arr_like,
                'quality': quality flags: arr_like,
                'orbitid': orbit id: arr_like,
                'sap_x': sap x positions: arr_like,
                'sap_y': sap y positions: arr_like,
                'sap_bkg': background fluxes: arr_like  ,
                'sap_bkg_err': background fluxes errors: arr_like,
                'kspsap_flux_sml': small KSP aperture fluxes: arr_like,
                'kspsap_flux_lag': lagged KSP aperture fluxes: arr_like,
                'RA': ra: float,
                'DEC': dec: float,
                'tess_mag': tess magnitude: float,
                'radius': stellar radius: float,
                'teff': stellar effective temperature: float,
                'logg': stellar logg: float,
                'mh': stellar metallicity: float
            }
        '''

        fits_fp = os.path.join(self.fits_dir, f'{self.sector_str}/{catalog_row["fp1"]}/{catalog_row["fp2"]}/{catalog_row["fp3"]}/{catalog_row["fp4"]}/hlsp_qlp_tess_ffi_{self.sector_str}-{f'{catalog_row["TIC_ID"]:016d}'}_tess_v01_llc.fits') #f'cam{catalog_row["cam"]}-ccd{catalog_row["ccd"]}/{catalog_row["fp1"]}/{catalog_row["fp2"]}/{catalog_row["fp3"]}/{catalog_row["fp4"]}/hlsp_qlp_tess_ffi_gaiaid-{catalog_row["gaiadr3_id"]}-{self.sector_str}-cam{catalog_row["cam"]}-ccd{catalog_row["ccd"]}_tess_v1_llc.fits')
        try:
            with fits.open(fits_fp, mode='readonly', memmap=True) as hdu:
                # see docs @ https://archive.stsci.edu/hlsps/qlp/hlsp_qlp_tess_ffi_all_tess_v1_data-prod-desc.pdf

                return {
                    'TIC_ID': catalog_row['TIC_ID'],
                    'time': hdu[1].data['time'],
                    'sap_flux': hdu[1].data['sap_flux'],
                    'kspsap_flux': hdu[1].data['kspsap_flux'],
                    'kspsap_flux_err': hdu[1].data['kspsap_flux_err'],
                    'quality': hdu[1].data['quality'],
                    'orbitid': hdu[1].data['orbitid'],
                    'sap_x': hdu[1].data['sap_x'],
                    'sap_y': hdu[1].data['sap_y'],
                    'sap_bkg': hdu[1].data['sap_bkg'],
                    'sap_bkg_err': hdu[1].data['sap_bkg_err'],
                    'kspsap_flux_sml': hdu[1].data['kspsap_flux_sml'],
                    'kspsap_flux_lag': hdu[1].data['kspsap_flux_lag'],
                    'RA': hdu[0].header['ra_obj'],
                    'DEC': hdu[0].header['dec_obj'],
                    'tess_mag': hdu[0].header['tessmag'],
                    'radius': hdu[0].header['radius'],
                    'teff': hdu[0].header['teff'],
                    'logg': hdu[0].header['logg'],
                    'mh': hdu[0].header['mh']
                }
            
        except FileNotFoundError:
            print(f"File not found: {fits_fp}")
            # Not sure why some files are not found in the tests
            return None

    def save_in_standard_format(self, args: tuple[Table, str]) -> bool:
        '''
        Save the standardised batch of light curves dict in a hdf5 file 

        Parameters
        ----------
        args: tuple, tuple of arguments: (subcatalog, output_filename)

        Returns
        -------
        success: bool, True if the file was saved successfully, False otherwise
        '''

        subcatalog, output_filename = args
        
        if not os.path.exists(os.path.dirname(output_filename)):
            os.makedirs(os.path.dirname(output_filename))

        results = [] 
        for row in tqdm(subcatalog):
            result = self.processing_fn(row)
            if result is not None: # Usually for files not found.
                results.append(result)

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

    def download_sh_script(self, show_progress: bool = False) -> bool:
        '''
        Download the sh script from the QLP MAST site ()

        Parameters
        ----------
        output_path: str, path to the output directory
        show_progress: bool, if True, show the progress of the download

        Returns
        -------
        success: bool, True if the file was downloaded successfully, False otherwise

        Raises
        ------
        Exception: if there is an error downloading the file
        ''' 

        #url = f'https://archive.stsci.edu/hlsps/tglc/download_scripts/hlsp_tglc_tess_ffi_{self.sector_str}_tess_v1_llc.sh'
        url = f'https://archive.stsci.edu/hlsps/qlp/download_scripts/hlsp_qlp_tess_ffi_{self.sector_str}_tess_v01_llc-fits.sh'
        
        try:
            # Check if file already exists
            output_file = os.path.join(self.qlp_data_path, f"{self.sector_str}_fits_download_script.sh")
            if os.path.exists(output_file):
                print(f"File already exists at {output_file}, skipping download.")
                return True
            
            os.makedirs(self.qlp_data_path, exist_ok=True)

            curl_cmd = f'wget {"--progress=bar:force --show-progress" if show_progress else ""} {url} -O {os.path.join(self.qlp_data_path, f"{self.sector_str}_fits_download_script.sh")}'
            
            if show_progress: # This could be cleaner
                result = subprocess.run(curl_cmd, shell=True, check=True, text=True)
            else:
                result = subprocess.run(curl_cmd, shell=True, check=True, text=True, capture_output=True)

            if result.returncode == 0:
                print(f"Successfully downloaded: {self.qlp_data_path}/{self.sector_str}_fits_download_script.sh")
                return True
            else:
                print(f"Error downloading file: {result.stderr}")
                return False
            
        except Exception as e:
            print(f"Error downloading .sh file from {url}: {e}")
            return False
        
    def download_target_csv_file(self, show_progress: bool = False) -> bool:
        '''
        Download the target list csv file from the QLP MAST site (https://archive.stsci.edu/hlsp/qlp)

        Parameters
        ----------
        output_path: str, path to the output directory
        show_progress: bool, if True, show the progress of the download

        Returns
        -------
        success: bool, True if the file was downloaded successfully, False otherwise

        '''
        #url = f'https://archive.stsci.edu/hlsps/tglc/target_lists/{self.sector_str}.csv'

        url = f'https://archive.stsci.edu/hlsps/qlp/target_lists/{self.sector_str}.csv'
        try:    
            output_file = os.path.join(self.qlp_data_path, f"{self.sector_str}_target_list.csv")
            if os.path.exists(output_file):
                print(f"File already exists at {output_file}, skipping download.")
                return True
            
            os.makedirs(self.qlp_data_path, exist_ok=True)
            fp = os.path.join(self.qlp_data_path, f"{self.sector_str}_target_list.csv")
                        
            curl_cmd = f'wget {"--progress=bar:force --show-progress" if show_progress else ""} {url} -O {fp}'
            
            if show_progress:
                result = subprocess.run(curl_cmd, shell=True, check=True, text=True)
            else:
                result = subprocess.run(curl_cmd, shell=True, check=True, text=True, capture_output=True)

            if result.returncode == 0:
                print(f"Successfully downloaded: {fp}")
                return True
            else:
                print(f"Error downloading file: {result.stderr}")
                return False
            
        except Exception as e:
            print(f"Error downloading csv file from {url}: {e}")
            return False
        
    def download_sector_catalog_lightcurves(
            self,
            catalog: Table
        ) -> list[bool]:
        '''
        Download the light curves for a given sector and save them in the standard format

        Parameters
        ----------
        tiny: bool, if True, only use a small sample of 100 objects for testing

        Returns
        -------
        results: list, list of tuples containing the success status and the message for each light curve download
        '''
        
        if not os.path.exists(self.fits_dir):
            os.makedirs(self.fits_dir, exist_ok=True)
        
        with Pool(self.n_processes) as pool:
            results = list(tqdm(pool.imap(self.get_fits_lightcurve, [row for row in catalog]), total=len(catalog)))
        
        if sum([result for result in results]) != len(catalog):
            print("There was an error in the parallel processing of the download of the fits files, some files may not have been downloaded.")

        return results

    def convert_fits_to_standard_format(self, catalog: Table) -> list[bool]:
        '''
        Convert the fits light curves to the standard format and save them in a hdf5 file

        Parameters
        ----------
        catalog: astropy.Table, sector catalog

        Returns
        -------
        results: list, list of booleans indicating the success of the conversion for each light curve
        '''

        catalog = catalog.group_by(['healpix']) # will this handle millions of light curves? Or is more batching required?

        map_args = []
        for group in catalog.groups: 
            group_filename = os.path.join(self.hdf5_output_dir, '{}/healpix={}/001-of-001.hdf5'.format("QLP", group['healpix'][0]))
            map_args.append((group, group_filename))

        with Pool(self.n_processes) as pool:
            results = list(tqdm(pool.imap(self.save_in_standard_format, map_args), total=len(map_args)))

        if sum(results) != len(map_args):
            print("There was an error in the parallel processing of the fits files to standard format, some files may not have been processed correctly")
        return results
    
    def clean_up(self, fits_dir: str = None, sh_dir: str = None, csv_dir: str = None) -> bool:
        '''
        Clean-up for the fits, .sh and .csv files to free up disk space after the parent sample has been built.

        Parameters
        ----------
        fits_dir: str, path to the fits directory
        sh_dir: str, path to the sh directory
        csv_dir: str, path to the csv directory

        Returns
        -------
        success: bool, True if the clean-up was successful, False otherwise
        '''
        if fits_dir is not None:
            pass
        if sh_dir is not None:

            pass
        if csv_dir is not None:
            pass
        return 1
    
    def batcher(self, seq: list, batch_size: int) -> list[list]:
        return (seq[pos:pos + batch_size] for pos in range(0, len(seq), batch_size))

    def batched_download(self, catalog: Table, tiny: bool) -> list[list[bool]]:
        if tiny:
            results = self.download_sector_catalog_lightcurves(catalog=catalog[:_TINY_SIZE])
        else:
            catalog_len = len(catalog)

            results = []
            for batch in tqdm(self.batcher(catalog, _BATCH_SIZE), total = catalog_len // _BATCH_SIZE):
                try:
                    results.append(self.download_sector_catalog_lightcurves(batch))
                    # Might be a good idea to do processing and clean-up here. 
                except Exception as e:
                    print(f"Error downloading light curves: {e}. Waiting {PAUSE_TIME} seconds before retrying...")
                    time.sleep(PAUSE_TIME)
                    results.append(self.download_sector_catalog_lightcurves(batch))
                
            if sum([result for result in results]) != catalog_len:
                print(f"There was an error in the bulk download of the fits files, {sum([result for result in results])} / {catalog_len} files have been successfully downloaded.")

        return results
    
    def download_sector(
            self,
            tiny: bool = True, 
            show_progress: bool = False,
            save_catalog: bool = True
    ) -> bool:
        '''
        Download the sector data from the QLP-MAST site and save it in the standard format 

        Parameters
        ----------
        tiny: bool, if True, only use a small sample of 100 objects for testing
        show_progress: bool, if True, show the progress of the download

        Returns
        -------
        success: bool, True if the download was successful, False otherwise
        '''
    
        # Download the sh file from the QLP site
        self.download_sh_script(show_progress) 

        # Download the target list csv file from the QLP site
        self.download_target_csv_file(show_progress)

        # Create the sector catalog
        catalog = self.create_sector_catalog(save_catalog = save_catalog, tiny = tiny)
        # Download the fits light curves using the sector catalog

        #self.batched_download(catalog, tiny) # To-DO: You can use the results to check if the download was successful

        n_files = 0
        for _, _, files in os.walk(self.fits_dir):
            n_files += len([f for f in files if f.endswith('.fits')])

        assert n_files == len(catalog), f"Expected {len(catalog)} .fits files in {self.fits_dir}, but found {n_files}"

        # Process fits to standard format
        if tiny:
            self.convert_fits_to_standard_format(catalog[:_TINY_SIZE])
        else:
            self.convert_fits_to_standard_format(catalog)

        # TO-DECIDE: clean-up of fits, .sh and .csv files

        return 1
    
if __name__ == '__main__':
    main()


