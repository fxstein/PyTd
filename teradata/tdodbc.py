"""An implementation of the Python Database API Specification v2.0 using Teradata ODBC."""

# The MIT License (MIT)
#
# Copyright (c) 2015 by Teradata
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#  
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#  
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import sys, ctypes, logging, threading, atexit, platform, re, collections

from . import util, datatypes
from .api import *  # @UnusedWildImport
    
logger = logging.getLogger(__name__)

# ODBC Constants
SQL_ATTR_ODBC_VERSION, SQL_OV_ODBC2, SQL_OV_ODBC3 = 200, 2, 3
SQL_ATTR_QUERY_TIMEOUT, SQL_ATTR_AUTOCOMMIT = 0, 102
SQL_NULL_HANDLE, SQL_HANDLE_ENV, SQL_HANDLE_DBC, SQL_HANDLE_STMT = 0, 1, 2, 3
SQL_SUCCESS, SQL_SUCCESS_WITH_INFO, SQL_ERROR, SQL_INVALID_HANDLE, SQL_NEED_DATA, SQL_NO_DATA = 0, 1, -1, -2, 99, 100
SQL_CLOSE, SQL_UNBIND, SQL_RESET_PARAMS = 0, 2, 3
SQL_PARAM_TYPE_UNKNOWN, SQL_PARAM_INPUT, SQL_PARAM_INPUT_OUTPUT, SQL_PARAM_OUTPUT = 0, 1, 2, 4
SQL_ATTR_PARAM_BIND_TYPE, SQL_ATTR_PARAMS_PROCESSED_PTR, SQL_ATTR_PARAM_STATUS_PTR, SQL_ATTR_PARAMSET_SIZE = 18, 21, 20, 22 
SQL_PARAM_BIND_BY_COLUMN = 0
SQL_NULL_DATA, SQL_NTS, SQL_IS_POINTER, SQL_IS_UINTEGER, SQL_IS_INTEGER = -1, -3, -4, -5, -6
SQL_C_BINARY, SQL_VARBINARY, SQL_LONGVARBINARY = -2, -3, -4
SQL_C_WCHAR, SQL_WVARCHAR, SQL_WLONGVARCHAR = -8, -9, -10 
SQL_DESC_TYPE_NAME = 14
SQL_COMMIT, SQL_ROLLBACK = 0, 1

SQL_STATE_DATA_TRUNCATED = '01004'
SQL_STATE_CONNECTION_NOT_OPEN = '08003'
SQL_STATE_INVALID_TRANSACTION_STATE = '25000'

SQLLEN = ctypes.c_ssize_t
SQLULEN = ctypes.c_size_t
SQLUSMALLINT = ctypes.c_ushort
SQLSMALLINT = ctypes.c_short
SQLINTEGER = ctypes.c_int
SQLBYTE = ctypes.c_ubyte
SQLWCHAR = ctypes.c_wchar
SQLRETURN = SQLSMALLINT
SQLPOINTER = ctypes.c_void_p
SQLHANDLE = ctypes.c_void_p

ADDR = ctypes.byref
PTR = ctypes.POINTER
SMALL_BUFFER_SIZE = 2 ** 12
LARGE_BUFFER_SIZE = 2 ** 20
TRUE = 1
FALSE = 0

odbc = None
hEnv = None
lock = threading.Lock()
pyVer = sys.version_info[0]
osType = platform.system()

# The amount of seconds to wait when submitting non-user defined SQL (e.g. set query bands, etc).
QUERY_TIMEOUT=120

if pyVer > 2:
    unicode = str  # @ReservedAssignment

if osType == "Darwin" or osType == "Windows":
    # Mac OSx and Windows
    _createBuffer = lambda l : ctypes.create_unicode_buffer(l)
    _inputStr = lambda s, l = None : None if s is None else ctypes.create_unicode_buffer((s if util.isString(s) else str(s)), l) 
    _outputStr = lambda s : s.value
    _convertParam = lambda s : None if s is None else (s if util.isString(s) else str(s)) 
else:
    # Unix/Linux
    _createBuffer = lambda l : ctypes.create_string_buffer(l)
    _inputStr = lambda s, l = None : None if s is None else ctypes.create_string_buffer((s if util.isString(s) else str(s)).encode('utf8'), l) 
    _outputStr = lambda s : unicode(s.raw.partition(b'\00')[0], 'utf8')
    _convertParam = lambda s : None if s is None else ((s if util.isString(s) else str(s)).encode('utf8')) 
    SQLWCHAR = ctypes.c_char
        
connections = []
def cleanupConnections():
    """Cleanup open connections."""
    if connections:
        logger.warn("%s open connections found on exit, attempting to close...", len(connections))
        for conn in list(connections):
            conn.close();
        
def getDiagnosticInfo (handle, handleType=SQL_HANDLE_STMT):
    """Gets diagnostic information associated with ODBC calls, particularly when errors occur.""" 
    info = []
    infoNumber = 1
    sqlState = _createBuffer(6)
    nativeError = SQLINTEGER()
    messageBuffer = _createBuffer(SMALL_BUFFER_SIZE)
    messageLength = SQLSMALLINT()
    while True:
        rc = odbc.SQLGetDiagRecW(handleType, handle, infoNumber, sqlState,
            ADDR(nativeError), messageBuffer, len(messageBuffer), ADDR(messageLength))
        if rc == SQL_SUCCESS_WITH_INFO and messageLength.value > ctypes.sizeof(messageBuffer):
            # Resize buffer to fit entire message.
            messageBuffer = _createBuffer(messageLength.value)
            continue;
        if rc == SQL_SUCCESS or rc == SQL_SUCCESS_WITH_INFO:
            info.append((_outputStr(sqlState), _outputStr(messageBuffer), abs(nativeError.value)))
            infoNumber += 1
        elif rc == SQL_NO_DATA:
            return info
        elif rc == SQL_INVALID_HANDLE:
            raise InterfaceError('SQL_INVALID_HANDLE', "Invalid handle passed to SQLGetDiagRecW.")
        elif rc == SQL_ERROR:
            raise InterfaceError("SQL_ERROR", "SQL_ERROR returned from SQLGetDiagRecW.")
        else:
            raise InterfaceError("UNKNOWN_RETURN_CODE", "SQLGetDiagRecW returned an unknown return code: %s", rc)
                
def checkStatus (rc, hEnv=SQL_NULL_HANDLE, hDbc=SQL_NULL_HANDLE, hStmt=SQL_NULL_HANDLE, method="Method", ignore=None):
    """ Check return status code and log any information or error messages.  If error is returned, raise exception."""
    sqlState = []
    logger.trace("%s returned status code %s", method, rc)
    if rc not in (SQL_SUCCESS, SQL_NO_DATA):
        if hStmt != SQL_NULL_HANDLE:
            info = getDiagnosticInfo(hStmt, SQL_HANDLE_STMT)
        elif hDbc != SQL_NULL_HANDLE:
            info = getDiagnosticInfo(hDbc, SQL_HANDLE_DBC)
        else:
            info = getDiagnosticInfo(hEnv, SQL_HANDLE_ENV)
        for i in info:
            sqlState.append(i[0])
            if rc == SQL_SUCCESS_WITH_INFO:
                logger.debug(u"{} succeeded with info:  [{}] {}".format(method, i[0], i[1]))
            elif not ignore or i[0] not in ignore:
                logger.debug(u"{} returned non-successful error code {}:  [{}] {}".format(method, rc, i[0], i[1]))
                raise DatabaseError(i[2], u"[{}] {}".format(i[0], i[1]), i[0])
            else:
                logger.debug(u"Ignoring return of {} from {}:  [{}] {}".format(rc, method, i[0], i[1]))
                # Breaking here because this error is ignored and info could contain older error messages.
                # E.g. if error was SQL_STATE_CONNECTION_NOT_OPEN, the next error would be the original connection error.
                break;
        if not info:
            logger.info("No information associated with return code %s from %s", rc, method)
    return sqlState



def prototype (func, *args):
    """Setup function prototype"""
    func.restype = SQLRETURN
    func.argtypes = args

def initFunctionPrototypes():
    """Initialize function prototypes for ODBC calls."""
    prototype(odbc.SQLAllocHandle, SQLSMALLINT, SQLHANDLE, PTR(SQLHANDLE)) 
    prototype(odbc.SQLGetDiagRecW, SQLSMALLINT, SQLHANDLE, SQLSMALLINT, PTR(SQLWCHAR), PTR(SQLINTEGER), PTR(SQLWCHAR), SQLSMALLINT, PTR(SQLSMALLINT))
    prototype(odbc.SQLSetEnvAttr, SQLHANDLE, SQLINTEGER, SQLPOINTER, SQLINTEGER)
    prototype(odbc.SQLDriverConnectW, SQLHANDLE, SQLHANDLE, PTR(SQLWCHAR), SQLSMALLINT, PTR(SQLWCHAR), SQLSMALLINT, PTR(SQLSMALLINT), SQLUSMALLINT)
    prototype(odbc.SQLFreeHandle, SQLSMALLINT, SQLHANDLE)
    prototype(odbc.SQLExecDirectW, SQLHANDLE, PTR(SQLWCHAR), SQLINTEGER)
    prototype(odbc.SQLNumResultCols, SQLHANDLE, PTR(SQLSMALLINT))
    prototype(odbc.SQLDescribeColW, SQLHANDLE, SQLUSMALLINT, PTR(SQLWCHAR), SQLSMALLINT, PTR(SQLSMALLINT), PTR(SQLSMALLINT), PTR(SQLULEN), PTR(SQLSMALLINT), PTR(SQLSMALLINT))
    prototype(odbc.SQLColAttributeW, SQLHANDLE, SQLUSMALLINT, SQLUSMALLINT, SQLPOINTER, SQLSMALLINT, PTR(SQLSMALLINT), PTR(SQLLEN))
    prototype(odbc.SQLFetch, SQLHANDLE)
    prototype(odbc.SQLGetData, SQLHANDLE, SQLUSMALLINT, SQLSMALLINT, SQLPOINTER, SQLLEN, PTR(SQLLEN))
    prototype(odbc.SQLFreeStmt, SQLHANDLE, SQLUSMALLINT)
    prototype(odbc.SQLPrepareW, SQLHANDLE, PTR(SQLWCHAR), SQLINTEGER)
    prototype(odbc.SQLNumParams, SQLHANDLE, PTR(SQLSMALLINT))
    prototype(odbc.SQLDescribeParam, SQLHANDLE, SQLUSMALLINT, PTR(SQLSMALLINT), PTR(SQLULEN), PTR(SQLSMALLINT), PTR(SQLSMALLINT))
    prototype(odbc.SQLBindParameter, SQLHANDLE, SQLUSMALLINT, SQLSMALLINT, SQLSMALLINT, SQLSMALLINT, SQLULEN, SQLSMALLINT, SQLPOINTER, SQLLEN, PTR(SQLLEN))
    prototype(odbc.SQLExecute, SQLHANDLE)
    prototype(odbc.SQLSetStmtAttr, SQLHANDLE, SQLINTEGER, SQLPOINTER, SQLINTEGER)
    prototype(odbc.SQLMoreResults, SQLHANDLE)
    prototype(odbc.SQLDisconnect, SQLHANDLE)
    prototype(odbc.SQLSetConnectAttr, SQLHANDLE, SQLINTEGER, SQLPOINTER, SQLINTEGER)
    prototype(odbc.SQLEndTran, SQLSMALLINT, SQLHANDLE, SQLSMALLINT)
    prototype(odbc.SQLRowCount, SQLHANDLE, PTR(SQLLEN))

def initOdbcLibrary(odbcLibPath = None):
    """Initialize the ODBC Library."""
    global odbc
    if odbc is None:            
        if osType == "Windows":
            odbc = ctypes.windll.odbc32
        else:
            if not odbcLibPath:
                # If MAC OSx
                if osType == "Darwin":
                    odbcLibPath = "libiodbc.dylib"
                else:
                    odbcLibPath = 'libodbc.so'
            logger.info("Loading ODBC Library: %s", odbcLibPath)
            odbc = ctypes.cdll.LoadLibrary(odbcLibPath) 
        
def initOdbcEnv():
    """Initialize ODBC environment handle."""
    global hEnv
    if hEnv is None:
        hEnv = SQLPOINTER()
        rc = odbc.SQLAllocHandle(SQL_HANDLE_ENV, SQL_NULL_HANDLE, ADDR(hEnv))
        checkStatus (rc, hEnv=hEnv)
        atexit.register(cleanupOdbcEnv)
        atexit.register(cleanupConnections)
        # Set the ODBC environment's compatibility level to ODBC 3.0
        rc = odbc.SQLSetEnvAttr(hEnv, SQL_ATTR_ODBC_VERSION, SQL_OV_ODBC3, 0)
        checkStatus(rc, hEnv=hEnv)
        
def cleanupOdbcEnv ():
    """Cleanup ODBC environment handle."""
    if hEnv:
        odbc.SQLFreeHandle(SQL_HANDLE_ENV, hEnv)
        
def init (odbcLibPath=None):
    try:
        lock.acquire()
        initOdbcLibrary(odbcLibPath)
        initFunctionPrototypes()
        initOdbcEnv()
    finally:
        lock.release()
    
class OdbcConnection:
    """Represents a Connection to Teradata using ODBC."""
    def __init__ (self, dbType="Teradata", system=None,
                  username=None, password=None, autoCommit=False,
                  transactionMode=None, queryBands=None, odbcLibPath=None, 
                  dataTypeConverter=datatypes.DefaultDataTypeConverter(), **kwargs):
        """Creates an ODBC connection."""
        self.hDbc = SQLPOINTER()
        self.cursorCount = 0
        self.sessionno = 0
        self.cursors = []
        self.dbType = dbType
        self.converter = dataTypeConverter
        connections.append(self)

        # Build connect string
        connectParams = collections.OrderedDict()
        connectParams["DRIVER"] = dbType
        if system:
            connectParams["DBCNAME"] = system
        if username:
            connectParams["UID"] = username
        if password:
            connectParams["PWD"] = password
        if transactionMode:
            connectParams["SESSIONMODE"] = "Teradata" if transactionMode == "TERA" else transactionMode
        connectParams.update(kwargs)
        connectString = u";".join(u"{}={}".format(key, value) for key,value in connectParams.items())
    
        # Initialize connection handle
        init(odbcLibPath)
        rc = odbc.SQLAllocHandle(SQL_HANDLE_DBC, hEnv, ADDR(self.hDbc))
        checkStatus(rc, hEnv=hEnv, method="SQLAllocHandle")
        
        # Create connection      
        logger.debug("Creating connection using ODBC ConnectString: %s", re.sub("PWD=.*?(;|$)", "PWD=XXX;", connectString))
        try:      
            lock.acquire()
            rc = odbc.SQLDriverConnectW(self.hDbc, 0, _inputStr(connectString), SQL_NTS, None, 0, None, 0);
        finally:
            lock.release()
        checkStatus(rc, hDbc=self.hDbc, method="SQLDriverConnectW")
        
        # Setup autocommit, query bands, etc.
        try:
            logger.debug("Setting AUTOCOMMIT to %s", "True" if util.booleanValue(autoCommit) else "False")
            rc = odbc.SQLSetConnectAttr(self.hDbc, SQL_ATTR_AUTOCOMMIT, TRUE if util.booleanValue(autoCommit) else FALSE, 0)
            checkStatus(rc, hDbc=self.hDbc, method="SQLSetConnectAttr - SQL_ATTR_AUTOCOMMIT")
            if dbType == "Teradata":
                with self.cursor() as c:
                    self.sessionno = c.execute("SELECT SESSION", queryTimeout=QUERY_TIMEOUT).fetchone()[0]
                    logger.debug("SELECT SESSION returned %s", self.sessionno);
                    if queryBands:
                        c.execute(u"SET QUERY_BAND = '{};' FOR SESSION".format(u";".join(u"{}={}".format(k, v) for k, v in queryBands.items())), queryTimeout=QUERY_TIMEOUT)
                self.commit()
                logger.debug("Created session %s.", self.sessionno)
        except Exception as e:
            self.close()
            raise e;
        
    def close (self):
        """CLoses an ODBC Connection."""
        if self.hDbc:
            if self.sessionno:
                logger.debug("Closing session %s...", self.sessionno)
            for cursor in list(self.cursors):
                cursor.close()
            rc = odbc.SQLDisconnect(self.hDbc)
            sqlState = checkStatus(rc, hDbc=self.hDbc, method="SQLDisconnect", ignore=[SQL_STATE_CONNECTION_NOT_OPEN, SQL_STATE_INVALID_TRANSACTION_STATE])
            if SQL_STATE_INVALID_TRANSACTION_STATE in sqlState:
                logger.warning("Rolling back open transaction for session %s so it can be closed.", self.sessionno)
                rc = odbc.SQLEndTran(SQL_HANDLE_DBC, self.hDbc, SQL_ROLLBACK)
                checkStatus(rc, hDbc=self.hDbc, method="SQLEndTran - SQL_ROLLBACK - Disconnect")    
                rc = odbc.SQLDisconnect(self.hDbc)
                checkStatus(rc, hDbc=self.hDbc, method="SQLDisconnect")
            rc = odbc.SQLFreeHandle(SQL_HANDLE_DBC, self.hDbc)
            if rc != SQL_INVALID_HANDLE:
                checkStatus(rc, hDbc=self.hDbc, method="SQLFreeHandle")    
            connections.remove(self)
            self.hDbc = None
            if self.sessionno:
                logger.debug("Session %s closed.", self.sessionno)
            
    def commit (self):
        """Commits a transaction."""
        logger.debug("Committing transaction...")
        rc = odbc.SQLEndTran(SQL_HANDLE_DBC, self.hDbc, SQL_COMMIT)
        checkStatus(rc, hDbc=self.hDbc, method="SQLEndTran - SQL_COMMIT")    
        
    def rollback (self):
        """Rollsback a transaction."""
        logger.debug("Rolling back transaction...")
        rc = odbc.SQLEndTran(SQL_HANDLE_DBC, self.hDbc, SQL_ROLLBACK)
        checkStatus(rc, hDbc=self.hDbc, method="SQLEndTran - SQL_ROLLBACK")    
    
    def cursor (self):
        """Returns a cursor."""
        cursor = OdbcCursor(self, self.dbType, self.converter, self.cursorCount)
        self.cursorCount += 1
        return cursor
        
    def __del__ (self):
        self.close()

    def __enter__ (self):
        return self
    
    def __exit__ (self, t, value, traceback):
        self.close()
        
    def __repr__(self):
        return "OdbcConnection(sessionno={})".format(self.sessionno)
    
connect = OdbcConnection

class OdbcCursor (util.Cursor):
    """Represents an ODBC Cursor."""
    def __init__(self, connection, dbType, converter, num):
        util.Cursor.__init__(self, connection, dbType, converter)
        self.num = num
        if num > 0:
            logger.debug("Creating cursor %s for session %s.", self.num, self.connection.sessionno);
        self.hStmt = SQLPOINTER()
        rc = odbc.SQLAllocHandle(SQL_HANDLE_STMT, connection.hDbc, ADDR(self.hStmt))
        checkStatus(rc, hStmt=self.hStmt);
        connection.cursors.append(self)
    
    def callproc (self, procname, params, queryTimeout=0):
        query = "CALL {} (".format(procname)
        for i in range(0, len(params)):
            if i > 0:
                query += ", "
            query += "?"
        query += ")"
        logger.debug("Executing Procedure: %s", query)
        self.execute(query, params, queryTimeout=queryTimeout)
        return util.OutParams (params, self.dbType, self.converter)
    
    def close(self):
        if self.hStmt:
            if self.num > 0:
                logger.debug("Closing cursor %s for session %s.", self.num, self.connection.sessionno);
            rc = odbc.SQLFreeHandle(SQL_HANDLE_STMT, self.hStmt)
            checkStatus(rc, hStmt=self.hStmt);
            self.connection.cursors.remove(self)
            self.hStmt = None
            
    def _setQueryTimeout (self, queryTimeout):
        rc = odbc.SQLSetStmtAttr(self.hStmt, SQL_ATTR_QUERY_TIMEOUT, SQLPOINTER(queryTimeout), SQL_IS_UINTEGER)
        checkStatus(rc, hStmt=self.hStmt, method="SQLSetStmtStmtAttr - SQL_ATTR_QUERY_TIMEOUT")
                
    def execute (self, query, params=None, queryTimeout=0):
        if params:
            self.executemany (query, [ params, ], queryTimeout)
        else:
            if self.connection.sessionno:
                logger.debug("Executing query on session %s using SQLExecDirectW: %s", self.connection.sessionno, query)
            self._free()
            self._setQueryTimeout(queryTimeout)
            rc = odbc.SQLExecDirectW(self.hStmt, _inputStr(_convertLineFeeds(query)), SQL_NTS)
            checkStatus(rc, hStmt=self.hStmt, method="SQLExecDirectW")
        self._handleResults()
        return self
    
    def executemany (self, query, params, batch=False, queryTimeout=0):
        self._free()
        # Prepare the query
        rc = odbc.SQLPrepareW(self.hStmt, _inputStr(_convertLineFeeds(query)), SQL_NTS)
        checkStatus(rc, hStmt=self.hStmt, method="SQLPrepare")
        self._setQueryTimeout(queryTimeout)
        # Get the number of parameters in the SQL statement.
        numParams = SQLSMALLINT()
        rc = odbc.SQLNumParams(self.hStmt, ADDR(numParams))
        checkStatus(rc, hStmt=self.hStmt, method="SQLNumParams")
        numParams = numParams.value
        # The argument types.
        dataTypes = []
        for paramNum in range(0, numParams):
            dataType = SQLSMALLINT() 
            parameterSize = SQLULEN()
            decimalDigits = SQLSMALLINT()
            nullable = SQLSMALLINT()
            rc = odbc.SQLDescribeParam(self.hStmt, paramNum + 1, ADDR(dataType), ADDR(parameterSize), ADDR(decimalDigits), ADDR(nullable))
            checkStatus(rc, hStmt=self.hStmt, method="SQLDescribeParams")
            dataTypes.append(dataType.value)
        if batch:     
            logger.debug("Executing query on session %s using batched SQLExecute: %s", self.connection.sessionno, query)
            self._executeManyBatch (params, numParams, dataTypes)
        else:
            logger.debug("Executing query on session %s using SQLExecute: %s", self.connection.sessionno, query)
            rc = odbc.SQLSetStmtAttr(self.hStmt, SQL_ATTR_PARAMSET_SIZE, 1, 0);
            checkStatus(rc, hStmt=self.hStmt, method="SQLSetStmtAttr")
            paramSetNum = 0
            for p in params:
                paramSetNum += 1
                logger.trace("ParamSet %s: %s", paramSetNum, p);
                if len(p) != numParams:
                    raise InterfaceError("PARAMS_MISMATCH", "The number of supplied parameters ({}) does not match the expected number of parameters ({}).".format(len(p), numParams))
                paramArray = []
                lengthArray = []
                valueType = SQL_C_WCHAR
                paramType = SQL_WVARCHAR
                for paramNum in range(0, numParams):
                    val = p[paramNum]
                    if dataTypes[paramNum] == SQL_WLONGVARCHAR:
                        paramType = SQL_WLONGVARCHAR
                    if isinstance(val, InOutParam):
                        param = _inputStr(val.inValue, SMALL_BUFFER_SIZE if val.size is None else val.size)
                        inputOutputType = SQL_PARAM_INPUT_OUTPUT
                        val.setValueFunc(lambda: _outputStr(param))
                    elif isinstance(val, OutParam):
                        param = _createBuffer(SMALL_BUFFER_SIZE if val.size is None else val.size)
                        inputOutputType = SQL_PARAM_OUTPUT
                        val.setValueFunc(lambda: _outputStr(param))          
                    elif isinstance(val, bytearray):
                        byteArr = SQLBYTE * len(val)
                        param = byteArr.from_buffer(val)
                        inputOutputType = SQL_PARAM_INPUT
                        valueType = SQL_C_BINARY
                        paramType = SQL_LONGVARBINARY
                    else:
                        param = _inputStr(val)
                        inputOutputType = SQL_PARAM_INPUT
                    paramArray.append(param)
                    if isinstance(val, bytearray):
                        numbytes = len(param)
                        bufSize = SQLLEN(numbytes)
                        lengthArray.append(SQLLEN(numbytes))
                    elif param is not None:
                        bufSize = SQLLEN(ctypes.sizeof(param))
                        lengthArray.append(SQLLEN(SQL_NTS))
                    else:
                        bufSize = SQLLEN(0)
                        lengthArray.append(SQLLEN(SQL_NULL_DATA))
                    columnSize = SQLULEN(0) if param is None else SQLULEN(len(param))
                    logger.trace("Binding parameter %s...", paramNum + 1)          
                    rc = odbc.SQLBindParameter(self.hStmt, paramNum + 1, inputOutputType, valueType, paramType, columnSize, 0, param, bufSize, ADDR(lengthArray[paramNum]))
                    checkStatus(rc, hStmt=self.hStmt, method="SQLBindParameter")
                logger.debug("Executing prepared statement.")
                rc = odbc.SQLExecute(self.hStmt)
                checkStatus(rc, hStmt=self.hStmt, method="SQLExecute")   
        self._handleResults()
        return self
    
    def _executeManyBatch (self, params, numParams, dataTypes):
        # Get the number of parameter sets.
        paramSetSize = len(params)   
        # Set the SQL_ATTR_PARAM_BIND_TYPE statement attribute to use column-wise binding.
        rc = odbc.SQLSetStmtAttr(self.hStmt, SQL_ATTR_PARAM_BIND_TYPE, SQL_PARAM_BIND_BY_COLUMN, 0);
        checkStatus(rc, hStmt=self.hStmt, method="SQLSetStmtAttr")
        # Specify the number of elements in each parameter array.
        rc = odbc.SQLSetStmtAttr(self.hStmt, SQL_ATTR_PARAMSET_SIZE, paramSetSize, 0);
        checkStatus(rc, hStmt=self.hStmt, method="SQLSetStmtAttr")
        # Specify a PTR to get the number of parameters processed. 
        #paramsProcessed = SQLULEN()
        #rc = odbc.SQLSetStmtAttr(self.hStmt, SQL_ATTR_PARAMS_PROCESSED_PTR, ADDR(paramsProcessed), SQL_IS_POINTER)
        #checkStatus(rc, hStmt=self.hStmt, method="SQLSetStmtAttr")
        # Specify a PTR to get the status of the parameters processed.
        #paramsStatus = (SQLUSMALLINT * paramSetSize)() 
        #rc = odbc.SQLSetStmtAttr(self.hStmt, SQL_ATTR_PARAM_STATUS_PTR, ADDR(paramsStatus), SQL_IS_POINTER)
        #checkStatus(rc, hStmt=self.hStmt, method="SQLSetStmtAttr")
        # Bind the parameters.
        paramArrays = []
        lengthArrays = []
        paramSetSize = len(params)      
        paramSetNum = 0
        for p in params:
            paramSetNum += 1
            logger.debug("ParamSet %s: %s", paramSetNum, p);
            if len(p) != numParams:
                raise InterfaceError("PARAMS_MISMATCH", "The number of supplied parameters ({}) does not match the expected number of parameters ({}).".format(len(p), numParams))
        for paramNum in range(0, numParams):
            p = []
            valueType = SQL_C_WCHAR
            paramType = SQL_WVARCHAR
            if dataTypes[paramNum] == SQL_WLONGVARCHAR:
                paramType = SQL_WLONGVARCHAR
            maxLen = 0
            for paramSetNum in range(0, paramSetSize):
                val = params[paramSetNum][paramNum]
                if isinstance(val, bytearray):
                    valueType = SQL_C_BINARY
                    paramType = SQL_LONGVARBINARY
                    l = len(val)
                    if l > maxLen:
                        maxLen = l
                else:    
                    val = _convertParam(val)
                    l = 0 if val is None else len(val)
                    if l > maxLen:
                        maxLen = l
                p.append(val)
            logger.debug("Max length for parameter %s is %s.", paramNum + 1, maxLen)
            if valueType == SQL_C_BINARY:
                valueSize = SQLLEN(maxLen)
                paramArrays.append((SQLBYTE * (paramSetSize * maxLen))())
            else:
                maxLen += 1
                valueSize = SQLLEN(ctypes.sizeof(SQLWCHAR) * maxLen)
                paramArrays.append(_createBuffer(paramSetSize * maxLen))
            lengthArrays.append((SQLLEN * paramSetSize)())
            for paramSetNum in range(0, paramSetSize):
                index = paramSetNum * maxLen
                if p[paramSetNum] is not None:
                    for c in p[paramSetNum]:
                        paramArrays[paramNum][index] = c
                        index += 1
                    if  valueType == SQL_C_BINARY:
                        lengthArrays[paramNum][paramSetNum] = len(p[paramSetNum])
                    else:
                        lengthArrays[paramNum][paramSetNum] = SQLLEN(SQL_NTS)
                        paramArrays[paramNum][index] = _convertParam("\x00")[0] 
                else:
                    lengthArrays[paramNum][paramSetNum] = SQLLEN(SQL_NULL_DATA)
                    paramArrays[paramNum][index] = _convertParam("\x00")[0]      
            logger.trace("Binding parameter %s...", paramNum + 1)
            rc = odbc.SQLBindParameter(self.hStmt, paramNum + 1, SQL_PARAM_INPUT, valueType, paramType, SQLULEN(maxLen), 0,
                                       paramArrays[paramNum], valueSize, lengthArrays[paramNum])
            checkStatus(rc, hStmt=self.hStmt, method="SQLBindParameter")
        # Execute the SQL statement.
        logger.debug("Executing prepared statement.")
        rc = odbc.SQLExecute(self.hStmt)
        checkStatus(rc, hStmt=self.hStmt, method="SQLExecute")   
    
    def _handleResults (self):
        # Rest cursor attributes.
        self.description = None
        self.rowcount = -1
        self.rownumber = None
        self.columns = {}
        self.types = []
        # Get column count in result set.
        columnCount = SQLSMALLINT()
        rc = odbc.SQLNumResultCols(self.hStmt, ADDR(columnCount))
        checkStatus(rc, hStmt=self.hStmt, method="SQLNumResultCols")
        rowCount = SQLLEN()
        rc = odbc.SQLRowCount(self.hStmt, ADDR(rowCount))
        checkStatus(rc, hStmt=self.hStmt, method="SQLRowCount")
        self.rowcount = rowCount.value;
        # Get column meta data and create row iterator.
        if columnCount.value > 0:    
            self.description = []
            nameBuf = _createBuffer(SMALL_BUFFER_SIZE)
            nameLength = SQLSMALLINT()
            dataType = SQLSMALLINT()
            columnSize = SQLULEN()
            decimalDigits = SQLSMALLINT()
            nullable = SQLSMALLINT()
            for col in range(0, columnCount.value):
                rc = odbc.SQLDescribeColW (self.hStmt, col + 1, nameBuf, len(nameBuf),
                    ADDR(nameLength), ADDR(dataType), ADDR(columnSize), ADDR(decimalDigits), ADDR(nullable))
                checkStatus(rc, hStmt=self.hStmt, method="SQLDescribeColW")
                columnName = _outputStr(nameBuf)
                odbc.SQLColAttributeW(self.hStmt, col + 1, SQL_DESC_TYPE_NAME, ADDR(nameBuf), len(nameBuf), None, None)
                checkStatus(rc, hStmt=self.hStmt, method="SQLColAttributeW")
                typeName = _outputStr(nameBuf)
                typeCode = self.converter.convertType(self.dbType, typeName)
                self.columns[columnName.lower()] = col
                self.types.append((typeName, typeCode))
                self.description.append((columnName, typeCode, None, columnSize.value, decimalDigits.value, None,
                                         nullable.value))
            self.iterator = rowIterator(self)
            
    def nextset (self):
        rc = odbc.SQLMoreResults(self.hStmt)
        checkStatus(rc, hStmt=self.hStmt, method="SQLMoreResults")
        if rc == SQL_SUCCESS or rc == SQL_SUCCESS_WITH_INFO:
            self._handleResults()
            return True
                                
    def _free (self):
        rc = odbc.SQLFreeStmt(self.hStmt, SQL_CLOSE)
        checkStatus(rc, hStmt=self.hStmt, method="SQLFreeStmt - SQL_CLOSE")
        rc = odbc.SQLFreeStmt(self.hStmt, SQL_RESET_PARAMS)
        checkStatus(rc, hStmt=self.hStmt, method="SQLFreeStmt - SQL_RESET_PARAMS")
        
def _convertLineFeeds (query):
    return "\r".join(util.sqlsplit(query, delimiter="\n"))
        
def rowIterator (cursor):
    """ Generator function for iterating over the rows in a result set. """
    buf = _createBuffer(LARGE_BUFFER_SIZE)
    bufSize = ctypes.sizeof(buf)
    length = SQLLEN()
    while True:
        rc = odbc.SQLFetch(cursor.hStmt)
        checkStatus(rc, hStmt=cursor.hStmt, method="SQLFetch")
        if rc == SQL_NO_DATA:
            break;
        values = []
        # Get each column in the row.
        for col in range(1, len(cursor.description) + 1):
            val = None
            binaryData = cursor.description[col - 1][1] == BINARY
            dataType = SQL_C_WCHAR
            if binaryData:
                dataType = SQL_C_BINARY
            rc = odbc.SQLGetData(cursor.hStmt, col, dataType, buf, bufSize, ADDR(length))
            sqlState = checkStatus(rc, hStmt=cursor.hStmt, method="SQLGetData")
            if length.value != SQL_NULL_DATA:
                if SQL_STATE_DATA_TRUNCATED in sqlState:
                    logger.debug("Data truncated. Calling SQLGetData to get next part of data for column %s of size %s.", col, length.value);
                    if binaryData:
                        val = bytearray(length.value)
                        val[0:bufSize] = (ctypes.c_ubyte * bufSize).from_buffer(buf)
                        newBufSize = len(val) - bufSize
                        newBuffer = (ctypes.c_ubyte * newBufSize).from_buffer(val, bufSize)
                        rc = odbc.SQLGetData(cursor.hStmt, col, dataType, newBuffer, newBufSize, ADDR(length))
                        checkStatus(rc, hStmt=cursor.hStmt, method="SQLGetData2")
                    else:
                        val = [_outputStr(buf), ]
                        while SQL_STATE_DATA_TRUNCATED in sqlState:
                            rc = odbc.SQLGetData(cursor.hStmt, col, dataType, buf, bufSize, ADDR(length))
                            sqlState = checkStatus(rc, hStmt=cursor.hStmt, method="SQLGetData2")
                            val.append(_outputStr(buf))
                        val = "".join(val)
                else:
                    if binaryData:
                        val = bytearray((ctypes.c_ubyte * length.value).from_buffer(buf))
                    else:
                        val = _outputStr(buf)
            values.append(val)
        yield values
    