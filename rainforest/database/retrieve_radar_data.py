#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Main routine for retrieving radar data
This is meant to be run as a command line command from a slurm script

i.e. ./retrieve_radar_data -t <task_file_name> -c <config_file_name>
- o <output_folder>

IMPORTANT: this function is called by the main routine in database.py
so you should never have to call it manually

"""

import numpy as np
import pandas as pd
import datetime
import logging
import gc
logging.basicConfig(level=logging.INFO)
import os

from collections import OrderedDict
from optparse import OptionParser

from rainforest.common import constants
from rainforest.common.lookup import get_lookup
from rainforest.common.utils import split_by_time, read_task_file, envyaml
from rainforest.common.utils import aggregate_multi, nested_dict_values
from rainforest.common.radarprocessing import Radar, hydroClass_single
from rainforest.common.retrieve_data import retrieve_prod, get_COSMO_T, get_COSMO_variables

IGNORE_ERRORS = True
                   
class Updater(object):
    def __init__(self, task_file, config_file, output_folder):
        """
        Creates an Updater  class instance that allows to add new radar data
        to the database
        
        Parameters
        ----------
        task_file : str
            The full path to a task file, i.e. a file with the following format
            timestamp, station1, station2, station3...stationN
            These files are generated by the database.py module so normally you
            shouldn't have to create them yourself
        config_file : str
            The full path of a configuration file written in yaml format
            that indicates how the radar retrieval must be done
        output_folder: str
            The full path where the generated files will be stored
        """
        self.config = envyaml(config_file)
        self.tasks = read_task_file(task_file)
        self.output_folder = output_folder
    
        # These are parameters that are used in many subfunctions
        self.radar_cfg = self.config['RADAR_RETRIEVAL']
        self.radars = self.radar_cfg['RADARS']
        self.radar_variables = self.radar_cfg['RADAR_VARIABLES']
        self.radar_variables.append('TCOUNT')
        self.cosmo_variables = self.radar_cfg['COSMO_VARIABLES']
        self.other_variables = self.radar_cfg['OTHER_VARIABLES']
        self.agg_methods = self.radar_cfg['AGGREGATION_METHODS']
        self.neighb_x = self.radar_cfg['NEIGHBOURS_X']
        self.neighb_y = self.radar_cfg['NEIGHBOURS_Y']
        self.sweeps = self.radar_cfg['SWEEPS']
        self.dims = {'nr':len(self.radars),
                     'nc':len(self.cosmo_variables),
                     'nrv':len(self.radar_variables),
                     'nnx':len(self.neighb_x),
                     'nny':len(self.neighb_y),
                     'no':len(self.other_variables),
                     'nm':len(self.agg_methods),
                     'ns':len(self.sweeps)}
        self.lut = {'coords':{}, 'heights':{}, 'visib': {}}
        for r in self.radars:
            coords, _, heights = get_lookup('station_to_rad', radar = r)
            self.lut['coords'][r], self.lut['heights'][r] = coords, heights
            self.lut['visib'][r] = get_lookup('visibility_rad', radar = r)
            
        if 'HYDRO' in self.radar_variables:
            # Note that hydro is treated a bit differently as it is computed
            # after aggregation to save time
            self.dims['nrv'] -= 1
            
    def retrieve_radar_files(self, radar, start_time, end_time, 
                             include_vpr = True, include_status = True):
        """
        Retrieves a set of radar files for a given time range
        
        Parameters
        ----------
        radar : char
            The name of the radar, i.e either 'A','D','L','P','W'
        start_time : datetime.datetime instance
            starting time of the time range
        end_time : datetime.datetime instance
            end time of the time range
        include_vpr : bool (optional)
            Whether or not to also include VPR files
        include_status : bool (optional)
            Whether or not to also include status files
        """
        
        sweeps = self.config['RADAR_RETRIEVAL']['SWEEPS']
      
        files_rad = {}
        files_rad['radar'] = {}
        if include_vpr:
            files_rad['vpr'] = {}
        

        try:
            files_r = retrieve_prod(self.config['TMP_FOLDER'], 
                                            start_time, end_time, 
                                            product_name = 'ML' + radar,
                                            sweeps = sweeps)
            files_rad['radar'] = files_r
            
            if include_vpr:
                if radar in ['L','A','D']:
                    radar_vpr = radar
                else:
                    radar_vpr = 'A'
                    
                # Take only one out of two since we work at 5 min
                files_v = retrieve_prod(self.config['TMP_FOLDER'], 
                                            start_time, end_time, 
                                            product_name = 'ZZ' + radar_vpr)
                files_rad['vpr'] = files_v[::2]
                
            if include_status:
               files_s = retrieve_prod(self.config['TMP_FOLDER'], 
                                        start_time, end_time, 
                                        product_name = 'ST' + radar,
                                        pattern = 'ST*')
               files_rad['status'] = files_s
               
            files_rad = split_by_time(files_rad)
        except:
            raise
            logging.error("""Retrieval for radar {:s} at timesteps {:s}-{:s} 
                      failed""".format(radar, str(start_time), str(end_time)))
        return files_rad

    def process_single_timestep(self, list_stations, radar_object, tidx):
        """
        Processes a single 5 min timestep for a set of stations
        
        Parameters
        ----------
        list_stations : list of str
            Names of all SMN or pluvio stations for which to retrieve the radar
            data
        radar_object : Radar object instance as defined in common.radarprocessing
            a radar object which contains all radar variables in polar format
        tidx : int
            indicates if a radar 5 min timestep is the first or the second
            in the corresponding 10 min gauge period, 1 = first, 2 = second     
        """
        
        # Some global parameters
        radar = radar_object.radname
        radsweeps = radar_object.radsweeps
      
        lut_coords = self.lut['coords'][radar]
        lut_heights = self.lut['heights'][radar]
        
        # For every timestep, we have this many columns per sweep
        ncols_by_sweep = (self.dims['no'] + self.dims['nc']
            + (self.dims['nnx'] * self.dims['nny'] * self.dims['nrv'])
            * self.dims['nm'])
  
        # Initialize output
        N, M = len(list_stations), ncols_by_sweep * len(self.sweeps)
        all_data = np.zeros((N,M), dtype = np.float32) + np.nan
        
        #################
        # Check if no processing is required, i.e. if no ZH at any station
        valid_data = False
        for sweep in radar_object.sweeps:
            ZH = radsweeps[sweep]
            for j, sta in enumerate(list_stations):
                if sta not in lut_coords.keys():
                    # Station not visible from given radar for that sweep
                    continue
                if sweep not in lut_coords[sta].keys():
                    # Station not visible from given radar for that sweep
                    continue
                for x in self.neighb_x:
                    for y in self.neighb_y:
                        strneighb = str(x)+str(y)
                        if strneighb not in lut_coords[sta][sweep].keys():
                            continue
                        if not len(lut_coords[sta][sweep][strneighb]):
                            continue
                            
                        idx = lut_coords[sta][sweep][strneighb]
                        if ZH.get_field(0,'ZH')[idx[:,0], idx[:,1]].count() > 0:
                            valid_data = True # al least one valid data

        
        #################
        if not valid_data:
            logging.info('No need to process radar {:s} no measurement above stations...'.format(radar))
            return  all_data
            
        # Censor file for SNR and visib, except for the visib field, which is kept as is
      
        if 'ZH_VISIB' in self.radar_variables  or 'ZV_VISIB' in self.radar_variables :
            radar_object.visib_mask(self.radar_cfg['VISIB_CORR']['MIN_VISIB'],
                          self.radar_cfg['VISIB_CORR']['MAX_CORR'])
            
        radar_object.snr_mask(self.radar_cfg['SNR_THRESHOLD'])
     
        # Compute KDP if needed
        if 'KDP' in self.radar_variables:
            radar_object.compute_kdp(self.radar_cfg['KDP_PARAMETERS'])

        # Compute attenuation correction if needed    
        if 'ZH_CORR' in self.radar_variables or 'ZDR_CORR' in self.radar_variables:
            radar_object.correct_attenuation()

        
        for sweep in radar_object.sweeps:
            #  i is an index going from 0 to number of sweeps in file
            # sweep is the actual sweep number, anything from 1 to 20visibility_rad
            logging.info('Sweep = ' + str(sweep))
            
            for j, sta in enumerate(list_stations):
                
                idx0_col = (sweep-1) * ncols_by_sweep

                if sta not in lut_coords.keys():
                    # Station not visible from given radar for all sweeps
                    continue
                if sweep not in lut_coords[sta].keys():
                    # Station not visible from given radar for that sweep
                    continue
            
                try:
                    if 'HEIGHT' in self.other_variables:
                        height = lut_heights[sta][sweep]
                         
                        all_data[j, idx0_col] = height
                        idx0_col += 1
                    
                    if 'VPR' in self.other_variables:
                        all_data[j,idx0_col] = float(radar_object.vpr(height))
                        idx0_col += 1
                    
                    if 'RADPRECIP' in self.other_variables:
                        # Get wet radome from status file
                        try:
                            wetradome = (radar_object.status['status']['sweep']
                                        [-1]['RADAR']['STAT']['WET_RADOME'])
                            
                            if wetradome == None:
                                radprecip = 0
                            else:
                                radprecip = float(wetradome['wetradome_mmh']['@value'])
                        except:
                            radprecip = np.nan
                            
                        all_data[j,idx0_col] = radprecip
                        idx0_col += 1
                        
                    # COSMO data
                    idx = lut_coords[sta][sweep]['00']
                    tmp = _data_at_station(radsweeps[sweep], 
                                           self.cosmo_variables,
                                           idx)
                    
                    all_data[j, idx0_col : idx0_col + self.dims['nc']] = tmp
                    idx0_col += self.dims['nc']
                    
                    for x in self.neighb_x:
                        for y in self.neighb_y:
                            strneighb = str(x)+str(y)
                            if strneighb not in lut_coords[sta][sweep].keys():
                                continue
                            if not len(lut_coords[sta][sweep][strneighb]):
                                continue
                            idx = lut_coords[sta][sweep][strneighb]
                            # Note that we need to use i and not sweep
                            # in get_field from pyart, because pyart
                            # does not know anything about the missing sweeps!
                            tmp = _data_at_station(radsweeps[sweep], 
                                                   self.radar_variables,
                                                   idx,
                                                   methods = self.agg_methods,
                                                   tidx = tidx)
                            all_data[j, idx0_col: idx0_col + len(tmp)] = tmp
                                 
                            idx0_col += len(tmp)

                except Exception as e:
                    logging.error(e)
                    logging.info('Ignoring exception...')
                    if IGNORE_ERRORS:
                        pass # can fail if only missing data 
                    else:
                        raise
        return all_data

    def process_all_timesteps(self):
        """
        Processes all timesteps that are in the task file
        """
        if 'HYDRO' in self.radar_variables:
            # Hydrometeor class is computed in a bit different way, only
            # after spatial and temporal aggregation
            compute_hydro = True
            self.radar_variables.remove('HYDRO')
        else:
            compute_hydro = False
        
        if 'VPR' in self.other_variables:
            include_vpr = True
        else: 

            include_vpr = False
        
        if ('PRECIPRAD' in self.other_variables or 
            'NH' in self.radar_variables or
            'NV' in self.radar_variables):
            include_status = True
        else:
            include_status = False
        
        # COSMO retrieval for T only is much faster...
        if self.cosmo_variables == ['T']:
            only_cosmo_T = True
        else:
            only_cosmo_T = False
            
        current_hour = None # Initialize current cour
        colnames = None # Initialize column nates

        # Create list of aggregation methods to use for aggregation in time 10 min
        # for every radar
        temp_agg_op = self.get_agg_operators()

        all_timesteps = list(self.tasks.keys())
        all_data_daily = []
        for i, tstep in enumerate(all_timesteps):
            
            logging.info('Processing timestep '+str(tstep))
            # Works at 10 min resolution 
            # retrieve radar data
            tstart = datetime.datetime.utcfromtimestamp(float(tstep))
            # Using six minutes ensures we include also the timesteps 5 min before
            tend = tstart + datetime.timedelta(minutes = 5)
            tstep_end = tstep + 10 * 60
            
            stations_to_get = self.tasks[tstep]

            hour_of_year = datetime.datetime.strftime(tstart,'%Y%m%d%H')
            day_of_year = hour_of_year[0:-2]
            
            if i == 0:
                current_day = day_of_year
  
            logging.info('---')
            if day_of_year != current_day or i == len(all_timesteps) - 1:
                logging.info('Saving new table for day {:s}'.format(str(current_day)))
                name = self.output_folder + current_day + '.parquet'
                try:
          
                    # Save data to file if end of loop or new day
                    
                    # Store data in new file
                    data = np.array(all_data_daily)

                    dic = OrderedDict()
      
                    for c, col in enumerate(colnames):
    
                        data_col = data[:,c]
                        # Check required column type
                        isin_listcols = [c == col.split('_')[0] for 
                                             c in constants.COL_TYPES.keys()]
                        if any(isin_listcols):
                            idx = np.where(isin_listcols)[0][0]
                            coltype = list(constants.COL_TYPES.values())[idx]
                            try:
                                data_col = data_col.astype(coltype)
                            except:# for int
                                data_col = data_col.astype(np.float).astype(coltype)
                        else:
                            data_col = data_col.astype(np.float32)
                                
                        dic[col] = data_col
                                                 
                    df = pd.DataFrame(dic)

                    # Remove duplicate rows
                    idx = 0
                    for m in self.agg_methods:
                        if idx == 0:
                            df['TCOUNT'] = df['TCOUNT_' + m] 
                        del df['TCOUNT_' + m]
                        
                    logging.info('Saving file ' + name)
                    df.to_parquet(name, compression = 'gzip', index = False)
                    
                except Exception as e:
                    logging.info('Could not save file ' + name)
                    logging.error(e)
                    if IGNORE_ERRORS:
                        pass # can fail if only missing data 
                    else:
                        raise   
                    
                # Reset list
                all_data_daily = []
                # Reset day counter
                current_day = day_of_year
                
            if len(self.cosmo_variables):
                if hour_of_year != current_hour:
                    current_hour = hour_of_year
                    try:
                        if only_cosmo_T :
                            cosmo_data = get_COSMO_T(tstart, self.sweeps)
                        else:
                            cosmo_data = get_COSMO_variables(tstart, 
                                     self.cosmo_variables, 
                                     self.sweeps, 
                                     tmp_folder = self.config['TMP_FOLDER'])
                          
                    except Exception as e:
                        logging.error(e)
                        logging.info('Ignoring exception...')
                        if IGNORE_ERRORS:
                            pass # can fail if only missing data 
                        else:
                            raise
            else:
                cosmo_data = None
  
            data_one_tstep = np.empty((len(stations_to_get),0), 
                                      dtype = np.float32)
            
            for r in self.radars: # Main loop
                # Check if we need to process the radar
                # If no station we want is in the list of stations seen by radar
                visible_stations = list(self.lut['coords']['A'].keys())
                if not np.any(np.isin(stations_to_get, visible_stations)):
                    logging.info('No need to process radar {:s} for these stations...'.format(r))
                    continue
                
                logging.info('Processing radar ' + r)
                try:
                    data_one_rad = []
                    
                    rad_files = self.retrieve_radar_files(r, tstart, tend, 
                                                          include_vpr,
                                                          include_status)

                    for tidx, tstamp in enumerate(rad_files['radar'].keys()): # 2 timesteps to make 10 min
                        # Create radar object
                        radar = Radar(r, rad_files['radar'][tstamp],
                                      rad_files['status'][tstamp],
                                      rad_files['vpr'][tstamp])
  
                        if len(self.cosmo_variables):
                            radar.add_cosmo_data(cosmo_data[r])
                      
                        tmp = self.process_single_timestep(stations_to_get,
                                                                  radar, tidx + 1)
                        data_one_rad.append(tmp)
                        del radar
                        gc.collect()
                        
                    # Now we aggregate in time over two periods of 5 min
                    # and add it in column direction
                    data_one_tstep = np.append(data_one_tstep, 
                                           aggregate_multi(np.array(data_one_rad),
                                                           temp_agg_op),
                                           axis = 1)
                except Exception as e:
                    logging.error(e)
                    logging.error("""Data retrieval for radar {:s} and timestep
                                    {:s} failed, assigning missing data
                                    """.format(r, str(tstep)))
                    
                    empty =  np.zeros((len(stations_to_get),
                                       len(temp_agg_op)),
                                       dtype = np.float32) + np.nan
                    
                    data_one_tstep = np.append(data_one_tstep, empty, axis = 1)
            
                    if IGNORE_ERRORS:
                        pass # can fail if only missing data 
                    else:
                        raise       

            
                # cleanup
                try:
                    all_files = nested_dict_values(rad_files)
                    for files in all_files:
                        if os.path.exists(files):
                            os.remove(files)
                except:
                    logging.error('Cleanup of radar data failed')
                    raise
    
            try:
                data_remapped, colnames = self._remap(data_one_tstep, tstep_end, 
                                                 stations_to_get,
                                                 compute_hydro)
                all_data_daily.extend(data_remapped)
                del data_remapped
            except Exception as e:
                logging.error(e)
                
                logging.info('Ignoring exception...')
                if IGNORE_ERRORS:
                    pass # can fail if only missing data 
                else:
                    raise       
                
            del data_one_tstep
            gc.collect()
             

                    
    
    def _remap(self, data, tstep, stations, compute_hydro = True):
        '''
        Remaps data from a format where all data from all sweeps and neighbours
        are in the same row to a format where every sweep is on a different row
        Original format
        |sweep 1|,|sweep 2|,|sweep 3|,...|sweep 20|
        where |...| = |OTHER_VARIABLES_SWEEPX, COSMO_VARIABLES_SWEEPX, RADAR_VARIABLES_SWEEPX]
        Output format
        a shape with one sweep, one neighbour per row
        TSTEP, STATION, SWEEP1, NX, NY, OTHER_VARIABLES_SWEEP1, COSMO_VARIABLES_SWEEP1, RADAR_VARIABLES_SWEEP1
        TSTEP, SSTATION, WEEP2, NX, NY, OTHER_VARIABLES_SWEEP2, COSMO_VARIABLES_SWEEP2, RADAR_VARIABLES_SWEEP2
        ...
        TSTEP, STATION, SWEEP20, NX, NY, OTHER_VARIABLES_SWEEP20, COSMO_VARIABLES_SWEEP20, RADAR_VARIABLES_SWEEP20
        
        Note that the timestep and station information are also added to the
        data
        
        Parameters
        ----------
        data : 2D numpy array
            data in original format with all sweeps and neighbours on one row
        tstep : str
            timestamp in str format
        stations : list of str
            list of all stations must have the same length as the data
        compute_hydro (optional):
            whether or not to compute the hydrometeor classification and add it
            to the data
        '''
        
        logging.info('Remapping to tabular format')
        
        rearranged = []
        for ii, row in enumerate(data): # Loop on stations
            cur_idx = 0
            for i in range(self.dims['nr']): # Loop on radar
                for j in range(self.dims['ns']): # Loop on sweeps
                    idx_sweep_start = cur_idx #  idx of the beginning of the sweep
                    cur_idx += self.dims['nc'] + self.dims['no'] # Current idx in sweep
                    for k in range(self.dims['nnx']):
                        for l in range(self.dims['nny']):
                            dslice = []
                            # Add to each row COSMO and OTHER vars from nx = ny = 0
                            dslice.extend(row[idx_sweep_start:
                                idx_sweep_start + self.dims['nc'] + self.dims['no']])
                            # and radar variables from nx = k, ny = l
                            dslice.extend(row[cur_idx: cur_idx + self.dims['nrv'] * 
                                                              self.dims['nm']])
                            dslice = np.array(dslice).astype(float)
                            if not np.any(np.isnan(dslice)) :
                                # Add constant info (timestamp, radars, sweep,
                                # nx, ny)
                                toAdd = [tstep,stations[ii]]
                                toAdd.extend([self.radars[i],self.sweeps[j],
                                              self.neighb_x[k],
                                              self.neighb_y[l]])
                                toAdd.extend(dslice)
           
                                rearranged.append(toAdd)
                                
                            # Update index
                            cur_idx += self.dims['nrv'] * self.dims['nm']

        cols = ['TIMESTAMP','STATION','RADAR','SWEEP','NX','NY']
        cols.extend(self.other_variables)
        cols.extend(self.cosmo_variables)
        for r in self.radar_variables:
            for m in self.agg_methods:
                cols.extend([r + '_' + m])
            
        rearranged = np.array(rearranged)
        if len(rearranged.shape) == 1:
            # If rearranged has only one line, expand to 2D
            rearranged = np.expand_dims(rearranged, axis = 0)

        if compute_hydro:
            logging.info('Computing hydrometeor classif')
            try:
                for m in self.agg_methods:
                    zh_idx = cols.index('ZH_'+m)
                    zdr_idx = cols.index('ZDR_'+m)
                    kdp_idx = cols.index('KDP_'+m)
                    rhohv_idx = cols.index('RHOHV_'+m)
                    T_idx = cols.index('T')
                    
                    hydro = hydroClass_single(rearranged[:,2], # radar
                                              rearranged[:,zh_idx].astype(float),
                                              rearranged[:,zdr_idx].astype(float),
                                              rearranged[:,kdp_idx].astype(float),
                                              rearranged[:,rhohv_idx].astype(float),
                                              rearranged[:,T_idx].astype(float))
                    rearranged = np.column_stack((rearranged, hydro))
                    cols.append('HYDRO_'+m)
            except: 
                    
                logging.error("""Could not compute hydrometeor classes, make 
                              sure that the variables ZH, ZDR, KDP, RHOHV and
                              T (COSMO temp) are specified in the config file       
                              """)
                raise # it will be caught later on
        return rearranged, cols
                
    def get_agg_operators(self):
        '''
        Returns all aggregation operators codes needed to aggregate all columns to
        10 min resolution, 0 = mean, 1 = log mean
        '''

        operators = []
        
        for o in self.other_variables:
            if o in constants.AVG_BY_VAR:
                operators.append(constants.AVG_BY_VAR[o])
            else:
                operators.append(0)
    
        for c in self.cosmo_variables:
            if c in constants.AVG_BY_VAR:
                operators.append(constants.AVG_BY_VAR[c])
            else:
                operators.append(0)
                
        operators_per_neighb = []
        for n1 in self.neighb_x:
            for n2 in self.neighb_y:
                for r in self.radar_variables:
                    for m in self.agg_methods:
                        if r in constants.AVG_BY_VAR:
                            operators_per_neighb.append(constants.AVG_BY_VAR[r])
                        else:
                             operators_per_neighb.append(0)
        operators.extend(operators_per_neighb)
        operators = operators * len(self.sweeps)
        
        return operators
    

def _data_at_station(radar_object, variables, idx, methods = ['mean'], tidx = None):
    '''
        Gets polar data at the location of a station, using the indexes of the
        lookup table
        
        Parameters
        ----------
        radar_object : Radar object instance as defined in common.radarprocessing
            a radar object which contains all radar variables in polar format
        variables : list of str
            list of all variables to get
        idx : list
            list of all polar indexes that correspond to the station
        methods (optional):
            which methods to use to aggregate polar data over the Cartesian
            pixel, available methods are 'mean', 'max', 'min'
        tidx : int
            indicates if a radar 5 min timestep is the first or the second
            in the corresponding 10 min gauge period, 1 = first, 2 = second            
    '''
    
    out = []

    if 'max' in methods or 'min' in methods or 'tcount' in variables:
         kdp = radar_object.get_field(0, 'KDP')[idx[:,0],idx[:,1]]
         zh =  radar_object.get_field(0, 'ZH')[idx[:,0],idx[:,1]]
         locmaxzh = np.ma.argmax(zh)
         locminzh = np.ma.argmin(zh)
         locmaxkdp = np.ma.argmax(kdp)
         locminkdp = np.ma.argmin(kdp)
         
    for v in variables:
        if v == 'HYDRO':
             continue # skip hydro is computed only after aggregation
        
        if v == 'TCOUNT':
            for m in methods:
                out.append(int(tidx * (zh.count() > 0)))
        else:
            data = np.ma.filled(radar_object.get_field(0,v)[idx[:,0],idx[:,1]],     
                                fill_value  = np.nan)
            for m in methods:
                if m == 'mean':
                    if v in constants.AVG_BY_VAR:
                        avg_method = constants.AVG_METHODS[constants.AVG_BY_VAR[v]]
                    else:
                        avg_method = constants.AVG_METHODS[0]
                        
                    tmp = avg_method(data, axis = None)
                    out.append(float(tmp))
            
                if m == 'max':
                    if v == 'KDP':
                        out.append(float(data[locmaxkdp]))
                    else:
                       out.append(float(data[locmaxzh]))
                    
                if m == 'min':
                    if v == 'KDP':
                        out.append(float(data[locminkdp]))
                    else:
                        out.append(float(data[locminzh]))
    return out

if __name__ == '__main__':
    parser = OptionParser()
    
    parser.add_option("-c", "--configfile", dest = "config_file",
                      help="Specify the user configuration file to use",
                      metavar="CONFIG")
    
    
    parser.add_option("-t", "--taskfile", dest = "task_file", default = None,
                      help="Specify the task file to process", metavar="TASK")
    
    parser.add_option("-o", "--output", dest = "output_folder", default = '/tmp/',
                      help="Specify the output directory", metavar="FOLDER")
    
    (options, args) = parser.parse_args()
    
    
    u = Updater(options.task_file, options.config_file, options.output_folder)
    u.process_all_timesteps()
    
    
