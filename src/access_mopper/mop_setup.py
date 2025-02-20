#!/usr/bin/env python
# Copyright 2023 ARC Centre of Excellence for Climate Extremes
# author: Paola Petrelli <paola.petrelli@utas.edu.au>
# author: Sam Green <sam.green@unsw.edu.au>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This is the ACCESS Model Output Post Processor, derived from the APP4
# originally written for CMIP5 by Peter Uhe and dapted for CMIP6 by Chloe Mackallah
# ( https://doi.org/10.5281/zenodo.7703469 )
#
# last updated 10/10/2024

import os
import shutil
import json
import csv
import click
import logging
from pathlib import Path
from json.decoder import JSONDecodeError
from importlib.resources import files as import_files

from mopper.setup_utils import (define_timeshot, adjust_nsteps,
    find_map_tables, write_var_map, write_table)
from mopper.cmip_utils import find_cmip_tables, read_dreq_vars
from mopdb.utils import read_yaml, write_yaml, MopException


def find_matches(table, var, realm, frequency, varlist):
    """Finds variable matching constraints given by table and config
    settings and returns a dictionary with the variable specifications. 

    CMOR variable can have more than one realm as 'ocean seaIce' 
    a mapping should just match one of them.
    NB. if an exact match (cmor name, realm, frequency is not found) 
    will try to find same cmor name, ignoring if time is point or mean,
    and realm but different frequency. This can then potentially be 
    resampled to desired frequency.

    Parameters
    ----------
    table : str
        Variable table 
    var : str
        Variable cmor/cmip style name to match
    realm : str
        Variable realm to match
    frequency : str
        Variable frequency to match
    varlist : list
        List of variables, each represented by a dictionary with mappings
        used to find a match to "var" passed 
    Returns
    -------
    match : dict
        Dictionary containing matched variable specifications
        or None if not matches

    """
    mop_log = logging.getLogger('mop_log')
    near_matches = []
    found = False
    match = None
    mop_log.debug(f"Looking for: {var}, {frequency}, {realm}")
    for v in varlist:
        mop_log.debug(f"{v['cmor_var']}, {v['frequency']}, {v['realm']}")
        if v['cmor_var'].startswith('#'):
            pass
        elif (v['cmor_var'] == var and v['realm'] in realm.split() 
              and v['frequency'] == frequency):
            match = v
            found = True
        elif (v['cmor_var'].replace('_Pt','') == var
              and v['realm'] in realm.split()):
            near_matches.append(v)
    if found is False and frequency != 'fx':
        v = find_nearest(near_matches, frequency)
        if v is not None:
            match = v
            found = True
        else:
            mop_log.info(f"could not find match for {table}-{var}" +
                f"-{frequency} check variables defined in mappings")
    if found is True:
        resample = match.get('resample', '')
        timeshot, frequency = define_timeshot(frequency, resample,
            match['cell_methods'])
        match['resample'] = resample
        match['timeshot'] = timeshot
        match['table'] = table
        match['frequency'] = frequency
        #if match['realm'] == 'land':
        #    realmdir = 'atmos'
        #else:
        #    realmdir = match['realm']
        in_fname = match['fpattern'].split()
        match['file_structure'] = ''
        for f in in_fname:
            #match['file_structure'] += f"/{realmdir}/{f}* "
            match['file_structure'] += f"**/{f}* "
    return match


def find_nearest(varlist, frequency):
    """Find variable with nearest frequency to resample.

    If variable is present in file at different frequencies,
    finds the one with higher frequency nearest to desired frequency.
    Adds frequency to variable resample field.
    Checks if modifier is present for frequency, match freq+mod must equal 
    var frequency, however modifier is removed to find resample frequency
    For valid resample frequency labels, see:
    https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#period-aliases

    Parameters
    ----------
    varlist : list
        Subset of variables with same realm and cmor name but different
        frequency
    frequency : str
        Variable frequency to match

    Returns
    -------
    var : dict
        Dictionary containing matched variable specifications
        or None if not matches

    """
    mop_log = logging.getLogger('mop_log')
    var = None
    found = False
    freq = frequency
    if 'Pt' in frequency:
        freq = frequency.replace('Pt','')
    elif 'C' in frequency:
        freq = frequency.replace('C','')
    resample_order = ['10yr', 'yr', 'mon', '10day', '7day',
            'day', '12hr', '6hr', '3hr', '1hr', '30min', '10min']
    resample_frq = {'10yr': '10Y', 'yr': 'Y', 'mon': 'M', '10day': '10D',
                    '7day': '7D', 'day': 'D', '12hr': '12h', '6hr': '6h',
                    '3hr': '3h', '1hr': 'h', '30min': '30min'}
    freq_idx = resample_order.index(freq)
    mop_log.debug(f"In find_nearest, freq: {freq}, freq_idx: {freq_idx}")
    for frq in resample_order[freq_idx+1:]:
        for v in varlist:
            vfrq = v['frequency'].replace('Pt','').replace('C','')
            mop_log.debug(f"Var: {v}, var frq: {vfrq}")
            if vfrq == frq:
                v['resample'] = resample_frq[freq]
                v['nsteps'] = adjust_nsteps(v, freq)
                found = True
                var = v
                break
        if found:
            break
    return var


@click.pass_context
def setup_env(ctx):
    """Sets up the configuration dictionary based on config file input

    Parameters
    ----------
    ctx : click context 
        Includes obj dict with 'cmor' settings, exp attributes

    Returns
    -------
    ctx : click context
        With updated dictionary including 'cmor' settings and
        attributes for experiment

    """
    mop_log = logging.getLogger('mop_log')
    cdict = ctx.obj
    cdict['appdir'] = Path(cdict['appdir'])
    appdir = cdict['appdir']
    if cdict['project'][0] == "$":
        cdict['project'] = os.getenv(cdict['project'][1:])
    mop_log.debug(f"appdir: {appdir}, {type(appdir)}")
    if cdict['outpath'] == 'default':
        cdict['outpath'] = (f"/scratch/{cdict['project']}/" + 
            f"{os.getenv('USER')}/MOPPER_output")
    if f"/{cdict['exp']}" not in cdict['outpath']:
        cdict['outpath'] = Path(cdict['outpath']) / cdict['exp']
    else:
        cdict['outpath'] = Path(cdict['outpath'])
    mop_log.debug(f"outpath: {cdict['outpath']}, {type(cdict['outpath'])}")
    cdict['master_map'] = appdir / cdict['master_map']
    if cdict['tables_path'] is None or cdict['tables_path'] == "":
        cdict['tables_path'] = appdir / "non-existing-path"
    else:
        cdict['tables_path'] = appdir / cdict['tables_path']
    cdict['ancils_path'] = appdir / cdict['ancils_path']
    # conda env to run job
    if cdict['conda_env'] == 'default':
        cdict['conda_env'] = ''
    else: 
        path =  Path(cdict['conda_env'])
        if not path.is_absolute():
            path = appdir / path
        cdict['conda_env'] = f"source {str(path)}"
    # Output subdirectories
    outpath = cdict['outpath']
    cdict['maps'] = outpath / "maps"
    cdict['tpath'] = outpath / "tables"
    cdict['cmor_logs'] = outpath / "cmor_logs"
    cdict['var_logs'] = outpath / "variable_logs"
    # Output files
    cdict['app_job'] = outpath / "mopper_job.sh"
    cdict['job_output'] = outpath / "job_output.OU"
    cdict['database'] = outpath / "mopper.db"
    # reference_date
    if cdict['reference_date'] == 'default':
        cdict['reference_date'] = (f"{cdict['start_date'][:4]}-" + 
            f"{cdict['start_date'][4:6]}-{cdict['start_date'][6:8]}")
    # make sure tstart and tend include hh:mm
    if len(cdict['start_date']) < 13:
        cdict['start_date'] += 'T0000'
        cdict['end_date'] += 'T0000'#'T2359'
    # if parent False set parent attrs to 'no parent'
    if cdict['attrs']['parent'] is False and cdict['mode'] == 'cmip6':
        p_attrs = [k for k in cdict['attrs'].keys() if 'parent' in k]
        for k in p_attrs:
            cdict['attrs'][k] = 'no parent'
    ctx.obj = cdict
    return ctx


#PP this is where dreq process start it can probably be simplified
# if we can read dreq as any other variable list
# and change year start end according to experiment
@click.pass_context
def var_map(ctx, activity_id=None):
    """Compares list of variables request by user to ones available
    in mappings file, call functions to define corresponding files.

    Calls create_var_map() for each identified table and finally write
    list of selected variables to yaml file for provenance.
      
    Parameters
    ----------
    ctx : click context 
        Includes obj dict with 'cmor' settings, exp attributes
    activity-id: str
        CMIP activity-id necessary only for CMIP style processing
        (default is None)
    Returns
    -------
    ctx : click context 
        Includes obj dict with 'cmor' settings, exp attributes
    """
    mop_log = logging.getLogger('mop_log')
    tables = ctx.obj.get('tables', 'all')
    subset = ctx.obj.get('var_subset', False)
    sublist = ctx.obj.get('var_subset_list', None)
    varsel = {}
    if subset is True:
        if sublist is None:
            mop_log.error("var_subset is True but file with variable list not provided")
            raise MopException("var_subset is True but file with variable list not provided")
        elif Path(sublist).suffix not in ['.yaml', '.yml']:
            mop_log.error(f"{sublist} should be a yaml file")
            raise MopException(f"{sublist} should be a yaml file")
        else:
            sublist = ctx.obj['appdir'] / sublist
    # Custom mode vars
    #if ctx.obj['mode'].lower() == 'custom':
    #    access_version = ctx.obj['access_version']
    if ctx.obj['force_dreq'] is True:
        if ctx.obj['dreq'] == 'default':
            ctx.obj['dreq'] = import_files('mopdata').joinpath( 
                'data/dreq/cmvme_all_piControl_3_3.csv' )
    with ctx.obj['master_map'].open(mode='r') as f:
        reader = csv.DictReader(f, delimiter=';')
        masters = list(reader)
    f.close()
    mop_log.info(f"Creating variable maps in directory '{ctx.obj['maps']}'")
    if subset:
        selection = read_yaml(sublist)
        tables = [t for t in selection.keys()]
        for table in tables:
            mop_log.info(f"\n{table}:")
            varsel = create_var_map(table, masters, varsel, selection=selection[table])
    elif tables.lower() == 'all':
        mop_log.info(f"Experiment {ctx.obj['exp']}: processing all tables")
        if ctx.obj['force_dreq']:
            tables = find_cmip_tables(ctx.obj['dreq'])
        else:
            tables = find_map_tables(masters)
        for table in tables:
            mop_log.info(f"\n{table}:")
            varsel = create_var_map(table, masters, varsel, activity_id)
    else:
        varsel = create_var_map(tables, varsel, masters)
    write_yaml(varsel, 'mop_var_selection.yaml', 'mop_log')
    return ctx


@click.pass_context
def create_var_map(ctx, table, mappings, varsel, activity_id=None, 
                   selection=None):
    """Create a mapping file for this specific experiment based on 
    model ouptut mappings, variables listed in table/s passed by config.
    Called by var_map

    Parameters
    ----------
    ctx : click context 
        Includes obj dict with 'cmor' settings, exp attributes
    table : str
        CMOR table to use
    mappings : dict
        Contains mappings as read from the csv map file
    varsel : dict
        Contains selection of variables to process
    activity-id: str
        CMIP activity-id necessary only for CMIP style processing
        (default is None)
    selection : list 
        List of variables to select in table, as passed by user
        with subset yaml file (default is None)

    Returns
    -------
    varsel : dict
        Contains updated selection of variables to process

    """
    mop_log = logging.getLogger('mop_log')
    matches = []
    fpath = ctx.obj['tables_path'] / f"{table}.json"
    if not fpath.exists():
         fpath = import_files('mopdata.cmor_tables').joinpath( 
             f"{table}.json")
    table_id = table.split('_')[1]
    mop_log.debug(f"Mappings: {mappings}")
    try:
        text = fpath.read_text()
        vardict = json.loads(text)
    except JSONDecodeError as e:
        mop_log.error(f"Invalid json {fpath}: {e}")
        raise MopException(f"Invalid json {fpath}: {e}")
    row_dict = vardict['variable_entry']
    all_vars = [v for v in row_dict.keys()]
    # work out which variables you want to process
    select = all_vars 
    if selection is not None:
        select = [v for v in all_vars if v in selection]
    elif ctx.obj['variable_to_process'] != 'all':
        select = [ctx.obj['variable_to_process']]
    elif ctx.obj['force_dreq'] is True:
        dreq_years = read_dreq_vars(table_id, activity_id)
        all_dreq = [v for v in dreq_years.keys()]
        select = set(select).intersection(all_dreq) 
    for var,row in row_dict.items():
        if var not in select:
            continue
        frq = row['frequency']
        realm = row['modeling_realm']
        years = 'all'
        if ctx.obj['force_dreq'] and var in all_dreq:
            years = dreq_years[var]
        if 'subhr' in frq:
            frq =  ctx.obj['subhr'] + frq.split('subhr')[1]
        match = find_matches(table, var, realm, frq, mappings)
        if match is not None:
            match['years'] = years
            matches.append(match)
    if matches == []:
        mop_log.info(f"{table}: no matching variables found")
    else:
        mop_log.info(f"    Found {len(matches)} variables")
        write_var_map(ctx.obj['maps'], table, matches)
        varsel[table] = [x['cmor_var'] for x in matches]
    write_table(table, vardict, select)
    return varsel


@click.pass_context
def archive_workdir(ctx):
    """If updating current post-processing move files
    to keep for provenance to "workidr#" folder. 

    Parameters
    ----------
    ctx : click context 
        Includes obj dict with 'cmor' settings, exp attributes

    Returns
    -------
    workdir : pathlib.Path
        Path of archived working directory

    """
    n = 1
    workdir = ctx.obj['outpath'] / f"workdir{str(n)}"
    while workdir.exists():
        n += 1  
        workdir = ctx.obj['outpath'] / f"workdir{str(n)}"
    workdir.mkdir()
    ctx.obj['maps'].rename(workdir / "maps")
    ctx.obj['tpath'].rename(workdir / "tables")
    ctx.obj['cmor_logs'].rename(workdir / "cmor_logs")
    ctx.obj['var_logs'].rename(workdir / "variable_logs")
    ctx.obj['app_job'].rename(workdir / "mopper_job.sh")
    ctx.obj['job_output'].rename(workdir / "job_output.OU")
     # move failed.csv and succes.csv if they exist
    for f in ctx.obj['outpath'].glob('*.csv'):
        f.rename(workdir / f.name)
    if ctx.obj['mode'] == 'cmip6':
        ctx.obj['json_file_path'].rename(workdir / ctx.obj['json_file_path'].name)
    return workdir


@click.pass_context
def manage_env(ctx):
    """Prepare working and directories. Removes or update pre-existing
    ones, based on user choice.

    Parameters
    ----------
    ctx : click context 
        Includes obj dict with 'cmor' settings, exp attributes

    """
    mop_log = logging.getLogger('mop_log')
    # check if output path already exists
    outpath = ctx.obj['outpath']
    if outpath.exists() and ctx.obj['update'] is False:
        answer = input(f"Output directory '{outpath}' exists.\n"+
                       "Delete and continue? [Y,n]\n")
        if answer == 'Y':
            try:
                shutil.rmtree(outpath)
            except OSError as e:
                raise MopException(f"Couldn't delete {outpath}: {e}")
        else:
            raise MopException("Aborting setup")
    # if updating working directory move files to keep
    if ctx.obj['update']:
        mop_log.info("Updating job_files directory...")
        workdir = archive_workdir()
    else:
        mop_log.info("Preparing job_files directory...")
    # Creating output directories
    ctx.obj['maps'].mkdir(parents=True)
    ctx.obj['tpath'].mkdir()
    ctx.obj['cmor_logs'].mkdir()
    ctx.obj['var_logs'].mkdir()
    # copy tables to working directory
    # check if present in tables_path or copy from packaged data
    # copy CV file as CMIP6_CV.json (remove when cmor bug is fixed)
    for f in ['_AXIS_ENTRY_FILE', '_FORMULA_VAR_FILE', 'grids',
         '_control_vocabulary_file']:
        fpath = ctx.obj['tables_path'] / ctx.obj[f]
        if not fpath.exists():
             fpath = import_files('mopdata.cmor_tables').joinpath(
                 f"{ctx.obj[f]}")
        if f == '_control_vocabulary_file':
            fname = "CMIP6_CV.json"
    # if updating make sure the CV file is not different
            if ctx.obj['update']:
                 fpath = workdir / "tables/CMIP6_CV.json"
        else:
            fname = ctx.obj[f]
        shutil.copyfile(fpath, ctx.obj['tpath'] / fname)
    update_code = import_files('mopdata').joinpath("update_db.py.txt")
    shutil.copyfile(update_code, ctx.obj['outpath'] / "update_db.py")
    return
